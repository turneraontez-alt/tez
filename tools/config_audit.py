#!/usr/bin/env python3
"""Config audit — AST inventory of environment-variable reads.

The codebase reads far more ``Q15_*`` env vars than ``.env.example`` documents.
This stdlib-only CLI scans every ``.py`` file (excluding ``.git``, ``tests/``
and any directory named ``data`` or ``work``) for env reads and keeps the gap
from growing:

* ``os.environ.get("NAME"[, default])`` / ``os.environ["NAME"]`` (Load only)
* ``os.getenv("NAME"[, default])``
* the repo's config-helper idiom — ANY call whose first positional argument is
  a string literal that is a full ``Q15_*`` name (``_bool("Q15_X", False)``,
  ``_float`` / ``_int`` / ``_str`` / ``_csv_set`` and friends) or one of the
  well-known non-Q15 env names (``TELEGRAM_BOT_TOKEN`` etc.).

Modes (exactly one required):
  --json            dump the full inventory (vars, defaults, read sites,
                    parse warnings, counts)
  --markdown        human-readable table sorted by var name
  --check           exit 1 listing vars neither documented in .env.example
                    nor listed in tools/config_baseline.json; exit 0 otherwise
  --write-baseline  regenerate tools/config_baseline.json from the current
                    undocumented set (freezes today's debt)

"Documented" means the var name appears anywhere in ``.env.example`` —
commented lines count.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directory names never scanned (matched at any depth). Dot-directories
# (.git, .claude, .venv, ...) are pruned separately.
EXCLUDED_DIR_NAMES = frozenset({"tests", "data", "work", "__pycache__", "node_modules"})

# Full-name pattern for the repo's env namespace, used by the helper-call
# heuristic (a bare string literal must be a complete Q15_* name to count).
Q15_NAME_RE = re.compile(r"Q15_[A-Z0-9_]+\Z")

# Non-Q15 env names accepted by the helper-call heuristic. Direct
# os.environ.get / os.getenv reads are recorded for ANY literal name, so this
# set only widens the heuristic for the handful of well-known credentials.
KNOWN_ENV_NAMES = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY_PATH",
        "DATABASE_URL",
        "PREDEXON_API_KEY",
        "GITHUB_TOKEN",
        "PORT",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ── scanning ──────────────────────────────────────────────────────────────────


def _is_environ_expr(node: ast.expr) -> bool:
    """True for expressions that denote os.environ (``os.environ`` or a bare
    ``environ`` imported from os)."""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id == "environ"


def _literal_str(node: Optional[ast.expr]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_repr(node: Optional[ast.expr]) -> Optional[str]:
    """repr() of a literal default expression, or None when absent/non-literal."""
    if node is None:
        return None
    try:
        return repr(ast.literal_eval(node))
    except (ValueError, SyntaxError):
        return None


def _default_arg(node: ast.Call) -> Optional[ast.expr]:
    """The default-value expression of an env-read call: 2nd positional arg,
    falling back to a ``default=`` keyword."""
    if len(node.args) >= 2:
        return node.args[1]
    for kw in node.keywords:
        if kw.arg == "default":
            return kw.value
    return None


def _classify_call(node: ast.Call) -> Optional[tuple[str, Optional[str]]]:
    """Return (var_name, default_repr) if this call reads an env var."""
    func = node.func
    first = _literal_str(node.args[0]) if node.args else None

    # os.environ.get("NAME"[, default]) — any literal name.
    if (
        isinstance(func, ast.Attribute)
        and func.attr in ("get", "setdefault", "pop")
        and _is_environ_expr(func.value)
    ):
        if first is None:
            return None
        return first, _literal_repr(_default_arg(node))

    # os.getenv("NAME"[, default]) — any literal name.
    is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
        isinstance(func, ast.Name) and func.id == "getenv"
    )
    if is_getenv:
        if first is None:
            return None
        return first, _literal_repr(_default_arg(node))

    # Helper heuristic: ANY call whose first positional arg is a full Q15_*
    # name (or a known env name). Covers _bool/_float/_int/_str/_csv_set and
    # every locally-defined variant of that idiom.
    if first is not None and (Q15_NAME_RE.fullmatch(first) or first in KNOWN_ENV_NAMES):
        return first, _literal_repr(_default_arg(node))
    return None


class _EnvReadVisitor(ast.NodeVisitor):
    """Collects (name, default_repr, lineno) triples from one module."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, Optional[str], int]] = []

    def visit_Call(self, node: ast.Call) -> None:
        classified = _classify_call(node)
        if classified is not None:
            name, default = classified
            self.reads.append((name, default, node.lineno))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ["NAME"] in Load context is a read; Store/Del are writes.
        if isinstance(node.ctx, ast.Load) and _is_environ_expr(node.value):
            name = _literal_str(node.slice)
            if name is not None:
                self.reads.append((name, None, node.lineno))
        self.generic_visit(node)


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield every .py file under root, pruning excluded and dot directories."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name.startswith(".") or name in EXCLUDED_DIR_NAMES:
                    continue
                stack.append(entry)
            elif entry.is_file() and name.endswith(".py"):
                yield entry


def scan_repo(root: Path) -> tuple[dict[str, dict], list[str]]:
    """Scan every eligible .py file under root.

    Returns (vars, warnings) where vars maps name ->
    {"name", "default", "defaults", "reads"}: ``defaults`` is every distinct
    literal-default repr seen, ``default`` is the sole entry when unambiguous,
    and ``reads`` is a sorted list of "relpath:line". ``warnings`` lists files
    skipped as unparseable.
    """
    root = root.resolve()
    found: dict[str, dict] = {}
    warnings: list[str] = []
    for path in iter_python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_bytes(), filename=rel)
        except (SyntaxError, ValueError) as exc:  # ValueError: e.g. null bytes
            warnings.append(f"{rel}: {exc.__class__.__name__}: {exc}")
            continue
        visitor = _EnvReadVisitor()
        visitor.visit(tree)
        for name, default, lineno in visitor.reads:
            entry = found.setdefault(
                name, {"name": name, "default": None, "defaults": [], "reads": []}
            )
            if default is not None and default not in entry["defaults"]:
                entry["defaults"].append(default)
            entry["reads"].append(f"{rel}:{lineno}")
    for entry in found.values():
        entry["defaults"].sort()
        entry["reads"] = sorted(
            set(entry["reads"]),
            key=lambda loc: (loc.rsplit(":", 1)[0], int(loc.rsplit(":", 1)[1])),
        )
        if len(entry["defaults"]) == 1:
            entry["default"] = entry["defaults"][0]
    return found, sorted(warnings)


# ── documentation / baseline ──────────────────────────────────────────────────


def load_documented_names(env_example: Path) -> frozenset[str]:
    """Every identifier token appearing anywhere in .env.example (comments
    count). Token-based so Q15_FOO never matches inside Q15_FOO_BAR."""
    try:
        text = env_example.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(text))


def load_baseline(baseline_path: Path) -> frozenset[str]:
    try:
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    undocumented = raw.get("undocumented", []) if isinstance(raw, dict) else []
    return frozenset(str(name) for name in undocumented)


def compute_undocumented(
    var_names: "set[str] | frozenset[str] | dict",
    documented: frozenset[str],
    baseline: frozenset[str] = frozenset(),
) -> list[str]:
    """Vars that are neither documented in .env.example nor baselined."""
    return sorted(n for n in var_names if n not in documented and n not in baseline)


def write_baseline(baseline_path: Path, undocumented: list[str]) -> None:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"undocumented": sorted(undocumented)}
    baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ── output ────────────────────────────────────────────────────────────────────


def build_inventory(root: Path) -> dict:
    variables, warnings = scan_repo(root)
    documented = load_documented_names(root / ".env.example")
    baseline = load_baseline(root / "tools" / "config_baseline.json")
    names = set(variables)
    inventory = {
        "root": str(root),
        "vars": [variables[name] for name in sorted(variables)],
        "counts": {
            "total": len(names),
            "documented": len(names & documented),
            "baselined": len(names & baseline),
            "undocumented_not_baselined": len(
                compute_undocumented(names, documented, baseline)
            ),
        },
        "warnings": warnings,
    }
    return inventory


def render_markdown(inventory: dict) -> str:
    lines = [
        "| Var | Default | Reads | First read |",
        "|---|---|---|---|",
    ]
    for entry in inventory["vars"]:
        default = entry["default"]
        if default is None and entry["defaults"]:
            default = " / ".join(entry["defaults"])
        default_cell = "" if default is None else default.replace("|", "\\|")
        lines.append(
            f"| {entry['name']} | `{default_cell}` | {len(entry['reads'])} "
            f"| {entry['reads'][0]} |"
        )
    counts = inventory["counts"]
    lines.append("")
    lines.append(
        f"{counts['total']} vars — {counts['documented']} documented, "
        f"{counts['baselined']} baselined, "
        f"{counts['undocumented_not_baselined']} undocumented+unbaselined."
    )
    if inventory["warnings"]:
        lines.append(f"Skipped unparseable files: {len(inventory['warnings'])}.")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="config_audit",
        description="Inventory env-var reads and enforce .env.example coverage.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repo root to scan (default: this checkout)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--json", action="store_true", help="dump the full inventory")
    mode.add_argument("--markdown", action="store_true", help="human table by name")
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any var is neither in .env.example nor the baseline",
    )
    mode.add_argument(
        "--write-baseline",
        action="store_true",
        help="regenerate tools/config_baseline.json from the undocumented set",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    variables, warnings = scan_repo(root)
    documented = load_documented_names(root / ".env.example")
    baseline_path = root / "tools" / "config_baseline.json"

    if args.json or args.markdown:
        inventory = build_inventory(root)
        print(json.dumps(inventory, indent=2) if args.json else render_markdown(inventory))
        return 0

    if args.write_baseline:
        undocumented = compute_undocumented(set(variables), documented)
        write_baseline(baseline_path, undocumented)
        print(
            f"Wrote {baseline_path} — {len(undocumented)} undocumented vars baselined "
            f"({len(variables)} total, {len(set(variables) & documented)} documented)."
        )
        return 0

    # --check
    baseline = load_baseline(baseline_path)
    missing = compute_undocumented(set(variables), documented, baseline)
    if warnings:
        for warning in warnings:
            print(f"warning: skipped {warning}", file=sys.stderr)
    if missing:
        print(
            f"{len(missing)} env var(s) are neither documented in .env.example "
            f"nor listed in {baseline_path.name}:"
        )
        for name in missing:
            first = variables[name]["reads"][0]
            print(f"  {name}  (first read: {first})")
        print(
            "Document them in .env.example or consciously baseline them with "
            "`python3 tools/config_audit.py --write-baseline`."
        )
        return 1
    print(
        f"OK — {len(variables)} env vars scanned; every one is documented in "
        f".env.example or baselined ({len(baseline & set(variables))} baselined)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Downstream pager/head closed the pipe (e.g. `--markdown | head`);
        # exit quietly instead of tracebacking.
        sys.stderr.close()
        sys.exit(0)
