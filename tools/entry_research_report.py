#!/usr/bin/env python3
"""Operator tool — Challenger research harness + Entry-Economics scoreboard.

Read-only. Two jobs, both safe to run on the Repl or against a learning-snapshot:

  1. ``challenger`` — evaluate the challenger v6 RESEARCH feature set against the
     previous (v5) challenger OUT-OF-SAMPLE, on the production ledger's settled
     rows. Prints the verdict and whether the upgrade clears the strict promotion
     gate (it never promotes anything — promotion stays a deliberate config switch).

  2. ``entry`` — print the Entry-Economics performance scoreboard from its own
     ledger (official ENTER-sent trades, decision counts, WAIT/SKIP studies).

Usage:
    python3 tools/entry_research_report.py challenger [PRODUCTION_LEDGER.sqlite3]
    python3 tools/entry_research_report.py entry [ENTRY_ECON_LEDGER.sqlite3]

Nothing here writes to any database or touches production state.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _challenger_research(ledger_path: str) -> int:
    from q15_upgrade.challenger import extract_bridge
    from q15_upgrade.challenger.config import ChallengerConfig
    from q15_upgrade.challenger.research import evaluate_feature_upgrade

    if not os.path.exists(ledger_path):
        print(f"production ledger not found: {ledger_path}")
        return 2
    tmp = ledger_path + ".research.jsonl"
    n = extract_bridge.export_production_ledger(ledger_path, tmp, resolved_only=True)
    rows = extract_bridge.load_jsonl(tmp)
    try:
        os.remove(tmp)
    except OSError:
        pass
    # The research harness needs the raw decision-time snapshots to extract BOTH
    # feature sets (v5 and v6), so rebuild them from the resolved rows and keep
    # (timestamp, snapshot, label) aligned and chronological.
    triples = sorted(
        [(float(r["created_at"]), extract_bridge._row_to_snapshot(r),
          1 if str(r.get("official_result")).upper() == "YES" else 0)
         for r in rows
         if str(r.get("official_result") or "").upper() in {"YES", "NO"}
         and r.get("created_at") is not None],
        key=lambda t: t[0])
    if not triples:
        print(f"no resolved rows in {ledger_path} (exported {n})")
        return 0
    ts = [t[0] for t in triples]
    snaps = [t[1] for t in triples]
    y = [t[2] for t in triples]
    cfg = ChallengerConfig.from_env()
    verdict = evaluate_feature_upgrade(ts, snaps, y, cfg)
    print(json.dumps({"resolved_rows": len(y), "verdict": verdict.as_dict()}, indent=2))
    print("\nPROMOTE v6?" , "YES — beats v5 OOS, significant" if verdict.promote
          else f"NO — keep in research ({verdict.reason})")
    return 0


def _entry_scoreboard(ledger_path: str) -> int:
    from q15_upgrade.entry_economics.config import EntryEconConfig
    from q15_upgrade.entry_economics.ledger import EntryLedger

    if not os.path.exists(ledger_path):
        print(f"entry-economics ledger not found: {ledger_path} "
              f"(enable Q15_ENTRY_ECON_LEDGER to start recording)")
        return 2
    cfg = EntryEconConfig.from_env()
    led = EntryLedger(ledger_path)
    try:
        out = {
            "model_version": cfg.model_version,
            "scoreboard": led.entry_scoreboard(cfg.model_version),
            "decision_counts": led.decision_counts(cfg.model_version),
            "background_studies": led.background_studies(cfg.model_version),
        }
    finally:
        led.close()
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"challenger", "entry"}:
        print(__doc__)
        return 1
    if argv[1] == "challenger":
        path = argv[2] if len(argv) > 2 else "data/q15_v95_ledger_v1.sqlite3"
        return _challenger_research(path)
    path = argv[2] if len(argv) > 2 else EntryEconDefaultPath()
    return _entry_scoreboard(path)


def EntryEconDefaultPath() -> str:
    from q15_upgrade.entry_economics.config import EntryEconConfig
    return EntryEconConfig.from_env().db_path


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
