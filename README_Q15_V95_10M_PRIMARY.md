# Q15 V9.5.1 — 10M Primary Learner Hotfix

This hotfix changes the learning priority without mixing 10M and 15M weights.

## Default behavior

- 10M has its own primary shadow challenger and learns from each unique,
  officially settled, sufficiently high-quality 10M prediction.
- 15M has a separate challenger but learning is disabled by default.
- The production champion remains frozen.
- No automatic promotion or threshold changes occur.
- Existing V9.5 rows are excluded from the new learner by model-version scope.
- Telegram reports show the 10M shadow probability as evaluation-only.

## Install

```bash
python q15_v9_5_10m_primary_learning_hotfix_installer.py artifacts/kalshi-monitor
```

Restart or redeploy after installation.

## Key safeguards

- One unique training row per contract/checkpoint.
- Official Kalshi settlement only.
- Checkpoint-isolated weights and pattern similarity.
- Low-quality rows do not train.
- Per-result and total-drift limits.
- Champion, 10M challenger, and 15M challenger remain separate.
- Manual review is required before any challenger becomes production.

## Existing endpoints

- `/api/q15-v9-5/diagnostics`
- `/api/q15-v9-5/predictions`
- `/api/q15-v9-5/calibration`
- `/api/q15-v9-5/learning`
- `/api/q15-v9-5/decision-stats`
