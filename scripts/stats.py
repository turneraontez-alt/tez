#!/usr/bin/env python3
"""All-stats dump for the V9.5 monitor — run this ON THE REPL (where data/ lives).

READ-ONLY. Prints, in one place:

  * ledger status (availability, model/feature-schema version, row counts);
  * the OFFICIAL win/loss record (15M/10M/7M, YES/NO, entry, manipulation) —
    graded only against settled outcomes;
  * the SHADOW FACTOR LAB — per-factor reliability attribution for every official
    prediction, with the promotion gate (reported, never applied).

It never writes production state, never changes a live prediction or entry, and
never touches the frozen champion or a real order.

Usage (on the Repl):
    python3 scripts/stats.py
    python3 scripts/stats.py --ledger data/q15_v95_ledger_v1.sqlite3
    python3 scripts/stats.py --checkpoint 10M --detail
    python3 scripts/stats.py --min-samples 30 --reliability 0.55
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade import factor_lab


def _pct(value: Any) -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _wl(bucket: Mapping[str, Any] | None) -> str:
    bucket = bucket or {}
    right = int(bucket.get("right") or 0)
    wrong = int(bucket.get("wrong") or 0)
    acc = bucket.get("accuracy")
    tail = " (low n)" if bucket.get("low_n") else ""
    return f"{right}W-{wrong}L {_pct(acc)}{tail}"


def _print_official(board: Mapping[str, Any]) -> None:
    print("OFFICIAL RECORD (sent predictions graded vs settlement)")
    if not board.get("available"):
        print("  unavailable (no ledger / no settled official predictions yet)")
        return
    for interval in ("15M", "10M", "7M"):
        grp = board.get(interval) or {}
        print(f"  {interval:<4} Y {_wl(grp.get('yes'))}   N {_wl(grp.get('no'))}   "
              f"T {_wl(grp.get('total'))}")
    print(f"  ENTRY  Y {_wl((board.get('entry') or {}).get('yes'))}   "
          f"N {_wl((board.get('entry') or {}).get('no'))}   "
          f"T {_wl((board.get('entry') or {}).get('total'))}")
    print(f"  MANIP  {_wl(board.get('manipulation'))}")


def _acc(value: Any) -> str:
    try:
        return "—" if value is None else f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _print_challenger(ledger: Any) -> None:
    """CHALLENGER learning, two layers, read-only:
      1) the shadow MODEL vs your current system (the 'Shadow vs Yours' package);
      2) the ledger's online champion-vs-challenger WEIGHT learning.
    Degrades gracefully when the challenger DB / shadow rows aren't there yet."""
    print("CHALLENGER — shadow model vs your system (read-only)")
    try:
        from q15_upgrade.challenger.config import ChallengerConfig
        cfg = ChallengerConfig()
        if not os.path.exists(cfg.db_path):
            print(f"  shadow model: no challenger DB yet ({cfg.db_path}) — "
                  "enable with Q15_CHALLENGER_ENABLED=true")
        else:
            from q15_upgrade.challenger.ledger import ShadowLedger
            sl = ShadowLedger(cfg.db_path)
            cmp = sl.comparison(model_version=cfg.model_version,
                                native_sent_only=cfg.native_sent_only)
            ov = cmp.get("overall", {})
            print(f"  model_version={cfg.model_version} · paired settled cases: {ov.get('n', 0)}")
            print(f"  accuracy  challenger {_acc(ov.get('challenger_accuracy'))}  "
                  f"vs your system {_acc(ov.get('current_accuracy'))}")
            for cp in ("15M", "10M", "7M"):
                b = (cmp.get("by_checkpoint") or {}).get(cp)
                if b:
                    print(f"    {cp:<4} challenger {_acc(b.get('challenger_accuracy'))}  "
                          f"vs yours {_acc(b.get('current_accuracy'))}  (n={b.get('n', 0)})")
            rk = sl.ranked_comparison(model_version=cfg.model_version,
                                      native_sent_only=cfg.native_sent_only)
            ch, na = rk.get("challenger", {}).get("overall", {}), rk.get("native", {}).get("overall", {})
            print(f"  ranked W/L  challenger {ch.get('correct', 0)}W-{ch.get('wrong', 0)}L "
                  f"{_acc(ch.get('accuracy'))}  ·  yours {na.get('correct', 0)}W-{na.get('wrong', 0)}L "
                  f"{_acc(na.get('accuracy'))}  ({rk.get('n_cases', 0)} cases)")
    except Exception as exc:  # never let the stats command die on the challenger
        print(f"  shadow model: unavailable ({type(exc).__name__}: {exc})")
    # Online weight learning (always available from the V9.5 ledger status).
    st = ledger.status() if hasattr(ledger, "status") else {}
    if st.get("available"):
        applied = st.get("shadow_updates_applied")
        by_cp = st.get("shadow_updates_by_checkpoint") or {}
        regimes = st.get("regime_challengers") or []
        active = sum(1 for r in regimes if r.get("active"))
        print("  online weights (champion frozen; challenger weights learn in shadow):")
        print(f"    shadow updates applied: {applied} "
              f"(by checkpoint: {', '.join(f'{k}={v}' for k, v in by_cp.items()) or '—'})")
        print(f"    regime challengers: {active}/{len(regimes)} active "
              f"(≥{st.get('minimum_regime_updates','?')} updates to activate)")
    print("  full out-of-sample verdict: python3 scripts/challenger_eval.py")


def _print_status(status: Mapping[str, Any]) -> None:
    print("LEDGER STATUS")
    if not status.get("available"):
        print("  unavailable")
        return
    print(f"  model_version={status.get('model_version')} "
          f"feature_schema={status.get('feature_schema_version')} "
          f"frozen={status.get('production_weights_frozen')} "
          f"auto_promotion={status.get('automatic_promotion')}")
    print(f"  predictions={status.get('unique_predictions')} "
          f"resolved={status.get('unique_resolved')} "
          f"(15M={status.get('fifteen_minute_predictions')} "
          f"10M={status.get('ten_minute_predictions')} "
          f"7M={status.get('seven_minute_predictions')})")
    if status.get("dropped_feature_rows"):
        print(f"  dropped_feature_rows={status.get('dropped_feature_rows')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="All V9.5 stats (read-only).")
    parser.add_argument("--ledger", help="path to the ledger SQLite file "
                        "(default: Q15_V95_LEDGER_DB / data/ default)")
    parser.add_argument("--checkpoint", choices=["15M", "10M", "7M"],
                        help="restrict the factor lab to one interval")
    parser.add_argument("--detail", action="store_true",
                        help="show per-interval reliability + why a factor is blocked")
    parser.add_argument("--top", type=int, default=12, help="factors to list (default 12)")
    parser.add_argument("--min-samples", type=int, default=factor_lab.DEFAULT_MIN_SAMPLES)
    parser.add_argument("--reliability", type=float, default=factor_lab.DEFAULT_RELIABILITY_THRESHOLD)
    parser.add_argument("--deadzone", type=float, default=factor_lab.DEFAULT_DEADZONE)
    args = parser.parse_args(argv)

    if args.ledger:
        os.environ["Q15_V95_LEDGER_DB"] = args.ledger

    # Imported AFTER the env override so the ledger opens the requested file.
    from q15_upgrade.ledger_v95 import V95Ledger

    ledger = V95Ledger()
    print("=" * 64)
    _print_status(ledger.status())
    print("-" * 64)
    _print_official(ledger.official_scoreboard())
    print("-" * 64)
    _print_challenger(ledger)
    print("-" * 64)
    rows = ledger.resolved_factor_rows(args.checkpoint)
    report = factor_lab.analyze(
        rows, deadzone=args.deadzone, min_samples=args.min_samples,
        reliability_threshold=args.reliability,
    )
    scope = f" [{args.checkpoint}]" if args.checkpoint else ""
    print(f"FACTOR LAB{scope}")
    for line in factor_lab.format_report(report, top=args.top, detail=args.detail):
        print(line)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
