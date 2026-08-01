# Q15 RTI microstructure numerical feature audit

- Status: `OUTCOME_BLIND_FIRST_FEATURE_REVIEW_READY`
- Design: `q15-rti-market-anchored-nested-safe-residual-v14`
- Fingerprint: `aa5efa9a986dc575ee4e358777cd2394b38550ad7328154b58a7d06bf55c3dda`
- Executable seven-asset windows: 34
- Windows remaining to first review: 0
- Outcome labels read: False
- Model fit: False
- Feature profile allow-list enforced: True
- Raw threshold JSON selected by CLI loader: False

| Cohort | Rows | Windows | Active | Rank | Stable rank | Projected train rows/active | High |r| pairs | Exact duplicates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 34 | 34 | 18 | 18 | 3.33 | 5.00 | 0 | 0 |
| NON_BTC_TRANSFER | 204 | 34 | 20 | 20 | 4.59 | 10.80 | 0 | 0 |

## Outcome-blind input integrity

- Status: `SOFT_DEGRADATION_PRESENT`
- Fully observed executable rows: 227/238
- Soft-degraded executable rows: 11
- Fully observed independent close windows: 24/34
- Soft-degraded independent close windows: 10
- Degradation by asset: {"BNB": 9, "HYPE": 2}
- Degradation by retained indicator: {"spot_flow_missing": 11}
- Exact-capture offset range: 0.0007808208465576172 to 0.5347425937652588 seconds
- Evidence-assembly lag range: 0.016015052795410156 to 2.0005738735198975 seconds
- Timing status: `OK`
- Frozen eligibility/readiness credit changed: False

This is an outcome-blind diagnostic. It cannot change the pinned design, fit a model, emit an artifact, send a notification, trade, refit, or promote.
