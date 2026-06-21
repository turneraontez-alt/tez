#!/usr/bin/env python3
"""Out-of-sample evaluation of the shadow challenger against the production ledger.

READ-ONLY. Run this ON THE REPL (where data/ lives) to answer two questions with
real settled data, before changing the model:

  1. How does the challenger do out-of-sample vs the market-only / volatility-only
     baselines AND vs the frozen champion (the control probability)?
  2. Does splitting the model PER CHECKPOINT (15M/10M/7M) beat one POOLED model?
     (i.e. is the architecture change worth it, given each split sees less data.)

It exports resolved rows from the production ledger, reconstructs the SAME
decision-time features the live shadow uses, runs a purged walk-forward, and
prints a comparison. It never trains the live shadow, never writes production
state, and never touches a real order.

Usage (on the Repl):
    python3 scripts/challenger_eval.py
    python3 scripts/challenger_eval.py --ledger data/q15_v95_ledger_v1.sqlite3
    python3 scripts/challenger_eval.py --min-rows 60 --splits 4
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.extract_bridge import (
    export_production_ledger,
    load_jsonl,
    rows_to_samples,
)
from q15_upgrade.challenger.harness import walk_forward_evaluate
from q15_upgrade.challenger.ledger import _prob_metrics
from q15_upgrade.challenger.mathx import clamp

CHECKPOINTS = ("15M", "10M", "7M")


def _champion_metrics(control: list[float | None], y: list[int]) -> dict[str, Any] | None:
    """Realized log-loss / Brier of the frozen champion (control probability).

    The champion is frozen, so its 'out-of-sample' performance is simply its
    realized performance on the resolved rows — a fair bar for the challenger's
    walk-forward metrics to clear."""
    pairs = [(c, yi) for c, yi in zip(control, y) if c is not None]
    if not pairs:
        return None
    p = [clamp(float(c), 1e-6, 1 - 1e-6) for c, _ in pairs]
    yy = [int(yi) for _, yi in pairs]
    return _prob_metrics(p, yy)


def _evaluate_slice(rows, checkpoint: str | None, config: ChallengerConfig) -> dict[str, Any]:
    ts, feats, y, control, _groups = rows_to_samples(rows, checkpoint=checkpoint)
    out: dict[str, Any] = {"n": len(y), "n_yes": sum(y), "n_no": len(y) - sum(y)}
    if len(y) < 2 or len(set(y)) < 2:
        out["ok"] = False
        out["reason"] = "too few rows / single class"
        return out
    out["champion"] = _champion_metrics(control, y)
    out["walk_forward"] = walk_forward_evaluate(ts, feats, y, config)
    return out


def evaluate_ledger(sqlite_path: str, *, model_version: str | None = None,
                    config: ChallengerConfig | None = None) -> dict[str, Any]:
    """Build the full pooled + per-checkpoint OOS report. READ-ONLY."""
    cfg = config or ChallengerConfig.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        dump = os.path.join(tmp, "resolved.jsonl")
        n = export_production_ledger(sqlite_path, dump, model_version=model_version,
                                     resolved_only=True)
        rows = load_jsonl(dump) if n else []

    report: dict[str, Any] = {"sqlite": sqlite_path, "resolved_rows": len(rows),
                              "pooled": {}, "per_checkpoint": {}}
    if not rows:
        report["note"] = "no resolved rows — let the monitor accumulate settled cases first"
        return report
    report["pooled"] = _evaluate_slice(rows, None, cfg)
    for cp in CHECKPOINTS:
        sub = _evaluate_slice(rows, cp, cfg)
        if sub["n"]:
            report["per_checkpoint"][cp] = sub
    return report


# ----------------------------- pretty printing -----------------------------
def _fmt_metrics(m: dict[str, Any] | None) -> str:
    if not m:
        return "      n/a"
    return (f"n={m['n']:<4} logloss={m['log_loss']:.4f}  brier={m['brier']:.4f}  "
            f"acc={m['accuracy']:.3f}  ece={m['ece']:.4f}")


def _print_slice(label: str, sl: dict[str, Any]) -> None:
    print(f"\n── {label}  ({sl['n']} rows: {sl.get('n_yes', '?')} YES / {sl.get('n_no', '?')} NO)")
    if not sl.get("champion") and not sl.get("walk_forward"):
        print(f"   skipped: {sl.get('reason', 'insufficient data')}")
        return
    print(f"   champion (frozen, control): {_fmt_metrics(sl.get('champion'))}")
    wf = sl.get("walk_forward") or {}
    if not wf.get("ok"):
        print(f"   challenger walk-forward: NOT RUN — {wf.get('reason', 'insufficient data')} "
              f"(n={wf.get('n', sl['n'])})")
        return
    print(f"   challenger (OOS, {wf.get('folds')} folds): {_fmt_metrics(wf.get('challenger'))}")
    print(f"   baseline market-only:       {_fmt_metrics(wf.get('market_only'))}")
    print(f"   baseline volatility-only:   {_fmt_metrics(wf.get('volatility_only'))}")
    ch, champ = wf.get("challenger"), sl.get("champion")
    if ch and champ:
        d_ll = champ["log_loss"] - ch["log_loss"]
        d_br = champ["brier"] - ch["brier"]
        verdict = ("BEATS champion" if (d_ll > 0 and d_br > 0)
                   else "trails champion" if (d_ll < 0 and d_br < 0)
                   else "mixed vs champion")
        print(f"   → challenger {verdict}: Δlogloss={d_ll:+.4f}  Δbrier={d_br:+.4f} "
              f"(positive = challenger better)")


def _print_report(report: dict[str, Any]) -> None:
    print("=" * 72)
    print(f"Challenger OOS evaluation — {report['sqlite']}")
    print(f"resolved rows: {report['resolved_rows']}")
    if report.get("note"):
        print(report["note"])
        return
    _print_slice("POOLED (all checkpoints, one model)", report["pooled"])
    if report["per_checkpoint"]:
        print("\n" + "=" * 72)
        print("PER-CHECKPOINT (does splitting the model help?)")
        for cp, sl in report["per_checkpoint"].items():
            _print_slice(f"checkpoint {cp}", sl)
    print("\n" + "=" * 72)
    print("How to read this: the challenger must clear BOTH the champion AND the\n"
          "market-only baseline on log-loss/Brier to be worth promoting. If the\n"
          "per-checkpoint splits don't beat POOLED, splitting isn't worth it yet\n"
          "(more likely you just need more settled data per checkpoint).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default="data/q15_v95_ledger_v1.sqlite3",
                    help="path to the production v95 ledger sqlite")
    ap.add_argument("--model-version", default=None,
                    help="filter production rows by model_version (default: all resolved)")
    ap.add_argument("--splits", type=int, default=None, help="walk-forward splits override")
    ap.add_argument("--min-rows", type=int, default=None,
                    help="min_train_rows override (lower it to evaluate on thin data)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.ledger):
        print(f"ledger not found: {args.ledger}\n"
              f"Run this on the Repl where data/ lives, or pass --ledger.", file=sys.stderr)
        return 2

    overrides: dict[str, Any] = {}
    if args.splits is not None:
        overrides["n_splits"] = args.splits
    if args.min_rows is not None:
        overrides["min_train_rows"] = args.min_rows
    cfg = ChallengerConfig.from_env()
    if overrides:
        cfg = cfg.with_overrides(**overrides)

    report = evaluate_ledger(args.ledger, model_version=args.model_version, config=cfg)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
