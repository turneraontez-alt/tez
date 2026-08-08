#!/usr/bin/env python3
"""Bundle the repo's source into ONE Markdown file an external code-audit
site (or another LLM) can read.

Why this exists: feeding a whole repo to a third-party auditor is painful when
the tree is full of noise — a checked-in virtualenv (``bin/``), binary images,
and multi-megabyte data snapshots. This walks the *git-tracked* files, drops
that noise, and emits a single self-describing Markdown document:

  * a project summary (lifted from CLAUDE.md),
  * a file manifest (what's in, with line counts),
  * an explicit "excluded" list (so the auditor knows nothing was hidden),
  * every included file under a ``## `path` `` heading in a fenced block.

Run:  python3 tools/build_audit_export.py [-o CODE_AUDIT.md] [--include-tests]
It is deterministic and read-only; it only writes the output file.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Whole directories we never bundle (venv, vcs, caches, binary asset dumps).
EXCLUDED_DIRS = (
    "bin/",            # checked-in virtualenv — not project code
    ".git/",
    "__pycache__/",
    "attached_assets/",  # screenshots / images
    "public/",           # static binary assets
    ".pytest_cache/",
)

# File extensions that are binary / not auditable as text.
EXCLUDED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".bin",
    ".sqlite3", ".db", ".gz", ".zip", ".pdf",
}

# Specific large data-dump files: real "data", not source. Excluded by default
# so the auditor sees code, not multi-megabyte JSON ledgers.
EXCLUDED_FILES = {
    "latest_snapshot.json",
    "v95_ledger_snapshot.json",
    "tez_review_dump.json",
}

# Map extension -> fenced-code language hint.
LANG = {
    ".py": "python", ".sh": "bash", ".bash": "bash", ".fish": "fish",
    ".ps1": "powershell", ".bat": "bat", ".nu": "nu", ".csh": "csh",
    ".js": "javascript", ".ts": "typescript", ".html": "html", ".css": "css",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".md": "markdown", ".txt": "text",
    ".sql": "sql", ".env": "bash", ".example": "bash", ".replit": "toml",
}

# Skip any single file larger than this many bytes (keeps the export sane).
MAX_FILE_BYTES = 400_000


def git_tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


def classify(rel: str) -> tuple[bool, str]:
    """Return (include?, reason-if-excluded)."""
    p = Path(rel)
    if any(rel.startswith(d) for d in EXCLUDED_DIRS):
        return False, "directory excluded (venv/cache/binary assets)"
    if p.name in EXCLUDED_FILES:
        return False, "large data snapshot (not source)"
    if p.suffix.lower() in EXCLUDED_EXTS:
        return False, f"binary/non-text type ({p.suffix})"
    abs_path = REPO / rel
    if not abs_path.is_file():
        return False, "missing/untracked on disk"
    size = abs_path.stat().st_size
    if size > MAX_FILE_BYTES:
        return False, f"too large ({size:,} bytes > {MAX_FILE_BYTES:,})"
    if is_binary(abs_path):
        return False, "binary content"
    return True, ""


def fence_for(text: str) -> str:
    """Pick a backtick fence longer than any run of backticks in the file,
    so embedded code blocks can't break out of the wrapper."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def project_summary() -> str:
    claude_md = REPO / "CLAUDE.md"
    if not claude_md.is_file():
        return ""
    lines = claude_md.read_text(encoding="utf-8", errors="replace").splitlines()
    # Grab the first prose paragraph after the title as a one-line summary.
    blurb = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            if blurb:
                break
            continue
        blurb.append(s)
        if len(blurb) >= 4:
            break
    return " ".join(blurb).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default="CODE_AUDIT.md",
                    help="output file (default: CODE_AUDIT.md)")
    ap.add_argument("--include-tests", action="store_true",
                    help="include the tests/ directory (excluded by default)")
    args = ap.parse_args()

    tracked = git_tracked_files()
    included: list[tuple[str, int, str]] = []  # (rel, lines, text)
    excluded: list[tuple[str, str]] = []

    for rel in sorted(tracked):
        if not args.include_tests and (rel == "tests" or rel.startswith("tests/")):
            excluded.append((rel, "tests/ (use --include-tests to add)"))
            continue
        keep, reason = classify(rel)
        if not keep:
            excluded.append((rel, reason))
            continue
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        included.append((rel, text.count("\n") + 1, text))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_lines = sum(n for _, n, _ in included)
    summary = project_summary()

    parts: list[str] = []
    w = parts.append

    w(f"# Code Audit Export — {REPO.name}\n")
    w(f"_Generated {stamp} • {len(included)} files • "
      f"{total_lines:,} lines of source_\n")
    if summary:
        w(f"\n**What this project is:** {summary}\n")
    w("\nThis single file bundles the project's source for external review. "
      "A checked-in virtualenv (`bin/`), binary assets, and large data "
      "snapshots were deliberately excluded — see the **Excluded** list so "
      "you can confirm nothing material was hidden. Each file appears under a "
      "`## \\`path\\`` heading in a fenced code block.\n")

    # Manifest
    w("\n## File manifest\n")
    for rel, n, _ in included:
        w(f"- `{rel}` — {n:,} lines")
    w("")

    # Excluded list (transparency)
    w("\n## Excluded (not in this export)\n")
    if excluded:
        # collapse the venv/cache spam into one bullet per reason group
        from collections import defaultdict
        groups: dict[str, list[str]] = defaultdict(list)
        for rel, reason in excluded:
            groups[reason].append(rel)
        for reason, rels in sorted(groups.items()):
            if len(rels) > 6:
                w(f"- {reason}: {len(rels)} files "
                  f"(e.g. `{rels[0]}`, `{rels[1]}`, …)")
            else:
                w(f"- {reason}: " + ", ".join(f"`{r}`" for r in rels))
    else:
        w("- (nothing)")
    w("")

    # File bodies
    w("\n## Source files\n")
    for rel, _, text in included:
        lang = LANG.get(Path(rel).suffix.lower(), "")
        fence = fence_for(text)
        w(f"\n### `{rel}`\n")
        w(f"{fence}{lang}")
        w(text.rstrip("\n"))
        w(fence)

    out_path = REPO / args.output
    out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    size = out_path.stat().st_size
    print(f"Wrote {out_path.relative_to(REPO)} — {len(included)} files, "
          f"{total_lines:,} lines, {size:,} bytes "
          f"({size / 1024:.0f} KB).")
    if excluded:
        print(f"Excluded {len(excluded)} files (venv/binaries/data/tests). "
              "See the Excluded section in the output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
