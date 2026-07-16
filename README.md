# Learning snapshots (auto-generated, do not edit)

This branch is force-pushed hourly by `tools/learning_export.py` on the
Repl. It is NOT part of `main` and is invisible to the GitHub Relay.

- Generated at: 2026-07-16T03:56:11.977934+00:00
- From commit: c78c4de
- Databases: q15_challenger_shadow_v1.sqlite3, q15_coinbase_adv_l2_v1.sqlite3, q15_drift_shadow_v1.sqlite3, q15_drift_telegram_outbox.sqlite3, q15_executor_orders_v1.sqlite3, q15_executor_yes_orders_v1.sqlite3, q15_high_vol_flip_v1.sqlite3, q15_interval_research_v1.sqlite3, q15_kraken_l3_v1.sqlite3, q15_ladder_probe_v1.sqlite3, q15_liq_feed_v1.sqlite3, q15_market_activity_v1.sqlite3, q15_marketlead_v1.sqlite3, q15_path_forecast_v1.sqlite3, q15_path_recorder_v1.sqlite3, q15_polymarket_shadow_v1.sqlite3, q15_settlement_index_v1.sqlite3, q15_spot_depth_v1.sqlite3, q15_strangle_shadow_v1.sqlite3, q15_strategy_bots_v3.sqlite3, q15_telegram_outbox.sqlite3, q15_ultoim_v1.sqlite3, q15_ultoim_v2_v1.sqlite3, q15_v94_context.sqlite3, q15_v94_telegram_gate_v1.sqlite3, q15_v94_unified_15m_learning_v3.sqlite3, q15_v95_ledger_v1.sqlite3

## Consume

Large/high-volume raw DB artifacts may be omitted by
`LEARNING_EXPORT_RAW_DB_EXCLUDE_NAMES` or when their gzip size exceeds
`LEARNING_EXPORT_MAX_ARTIFACT_BYTES`; `learning_snapshot.json` still
records row counts, available size metadata, and the skip reason.

```bash
git fetch origin learning-snapshots
git show origin/learning-snapshots:learning_snapshot.json | less
# raw ledgers for matched-row SQL:
mkdir -p /tmp/ledgers
git show origin/learning-snapshots:dbs/q15_v95_ledger_v1.sqlite3.gz \
  | gunzip > /tmp/ledgers/q15_v95_ledger_v1.sqlite3
```
