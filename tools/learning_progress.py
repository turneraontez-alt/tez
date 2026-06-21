#!/usr/bin/env python3
"""Read-only learning-progress report — run this ON THE REPL (where the live DBs
live) to dump the current state of every learning subsystem in one shot.

    python3 tools/learning_progress.py          # human-readable
    python3 tools/learning_progress.py --json    # machine-readable (one JSON blob)

It opens the SQLite ledgers directly and calls the SAME report builders the
dashboard's /api/q15-v9-5/* endpoints use, so the numbers match the app exactly.
It NEVER writes: it only reads, and it skips a store that doesn't exist on disk
rather than creating an empty one. Nothing here can touch a real exchange.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the repo importable whether run from the root or from tools/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _v95_section() -> dict:
    """Production v95 ledger: calibration, scoreboard, accuracy, learning."""
    from q15_upgrade.ledger_v95 import V95Ledger  # type: ignore
    db_path = os.environ.get("Q15_V95_LEDGER_DB") or "data/q15_v95_ledger_v1.sqlite3"
    if not os.path.exists(db_path):
        return {"available": False, "reason": f"no ledger at {db_path}"}
    led = V95Ledger()
    try:
        from q15_upgrade.accuracy_report import build_accuracy_report
        metrics = led.metrics()
        return {
            "available": True,
            "db_path": db_path,
            "status": led.status(),
            "scoreboard": led.scoreboard(),
            "accuracy": build_accuracy_report(metrics),
            "calibration": metrics,
            "shadow_signal_experiment": led.shadow_signal_experiment(),
        }
    finally:
        try:
            led.close()
        except Exception:
            pass


def _shadow_section() -> dict:
    """Shadow-vs-Yours challenger: out-of-sample scoreboard + native delivery audit."""
    from q15_upgrade.challenger.config import ChallengerConfig
    from q15_upgrade.challenger.ledger import ShadowLedger
    cfg = ChallengerConfig()
    if not os.path.exists(cfg.db_path):
        return {"available": False, "reason": f"no shadow DB at {cfg.db_path} "
                "(challenger may be disabled or has not recorded yet)"}
    led = ShadowLedger(cfg.db_path)
    try:
        mv = cfg.model_version
        return {
            "available": True,
            "db_path": cfg.db_path,
            "model_version": mv,
            "scoreboard": led.scoreboard(model_version=mv),
            "ranked_comparison": (led.ranked_comparison(model_version=mv)
                                  if hasattr(led, "ranked_comparison") else None),
            "delivery_audit": led.delivery_audit(model_version=mv),
        }
    finally:
        try:
            led.close()
        except Exception:
            pass


def collect() -> dict:
    out: dict = {}
    for name, fn in (("production_v95", _v95_section), ("shadow_vs_yours", _shadow_section)):
        try:
            out[name] = fn()
        except Exception as exc:  # never let one subsystem sink the report
            out[name] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


# ---- human-readable rendering -------------------------------------------------

def _fmt_pct(x) -> str:
    return "—" if x is None else f"{float(x) * 100:.1f}%"


def _render(report: dict) -> str:
    L: list[str] = []
    L.append("=" * 64)
    L.append("LEARNING PROGRESS")
    L.append("=" * 64)

    v95 = report.get("production_v95", {})
    L.append("\n## Production v95 ledger")
    if not v95.get("available"):
        L.append(f"  (unavailable: {v95.get('reason') or v95.get('error')})")
    else:
        st = v95.get("status") or {}
        L.append(f"  predictions recorded : {st.get('unique_predictions', '?')}")
        L.append(f"  resolved / settled   : {st.get('unique_resolved', '?')}")
        L.append(f"  shadow weight updates: {st.get('shadow_updates_applied', '?')} "
                 f"(last step Δ={st.get('last_learning_step_magnitude', '—')})")
        L.append(f"  last shadow update   : {st.get('last_shadow_update') or '—'}")
        sb = v95.get("scoreboard") or {}
        overall = sb.get("overall") or {}
        if overall:
            L.append(f"  overall accuracy     : {_fmt_pct(overall.get('accuracy'))} "
                     f"(right={overall.get('right', 0)} wrong={overall.get('wrong', 0)} "
                     f"n={overall.get('n', 0)}, pnl={overall.get('realized_total_cents', '—')}¢)")
        for iv in ("15M", "10M", "7M"):
            row = (sb.get("by_checkpoint") or {}).get(iv) or {}
            if row and row.get("n"):
                ci = (f" ci=[{_fmt_pct(row.get('ci_low'))},{_fmt_pct(row.get('ci_high'))}]"
                      if row.get("ci_low") is not None else "")
                L.append(f"    {iv:<4} acc={_fmt_pct(row.get('accuracy'))} n={row.get('n')}"
                         f" pnl={row.get('realized_total_cents', '—')}¢{ci}")
        acc = v95.get("accuracy") or {}
        if acc.get("headline") or acc.get("summary"):
            L.append(f"  readout              : {acc.get('headline') or acc.get('summary')}")

    sh = report.get("shadow_vs_yours", {})
    L.append("\n## Shadow-vs-Yours challenger")
    if not sh.get("available"):
        L.append(f"  (unavailable: {sh.get('reason') or sh.get('error')})")
    else:
        L.append(f"  model_version        : {sh.get('model_version')}")
        sb = sh.get("scoreboard") or {}
        L.append(f"  resolved shadow preds: {sb.get('resolved', 0)}")
        if sb.get("resolved"):
            ch, ct = sb.get("challenger") or {}, sb.get("control") or {}
            L.append(f"  brier  challenger={ch.get('brier', '—')}  control={ct.get('brier', '—')}")
            tr = sb.get("trading") or {}
            L.append(f"  trades={tr.get('n_trades', 0)}  win_rate={_fmt_pct(tr.get('win_rate'))}  "
                     f"pnl_cents={tr.get('total_hypothetical_pnl_cents', '—')}")
        else:
            L.append(f"  note                 : {sb.get('note', 'no resolved data yet')}")
        da = sh.get("delivery_audit") or {}
        L.append(f"  delivery audit       : SENT={da.get('SENT', 0)}  "
                 f"FAILED={da.get('DELIVERY_FAILED', 0)}  PENDING={da.get('PENDING', 0)}")

    L.append("\n" + "=" * 64)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the raw JSON blob")
    args = ap.parse_args()
    report = collect()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
