# Q15 RTI microstructure numerical feature audit

- Status: `PREVIEW_ONLY_BEFORE_30_WINDOWS`
- Design: `q15-rti-market-anchored-nested-safe-residual-v14`
- Fingerprint: `aa5efa9a986dc575ee4e358777cd2394b38550ad7328154b58a7d06bf55c3dda`
- Executable seven-asset windows: 1
- Windows remaining to first review: 29
- Outcome labels read: False
- Model fit: False
- Feature profile allow-list enforced: True
- Raw threshold JSON selected by CLI loader: False

| Cohort | Rows | Windows | Active | Rank | Stable rank | Projected train rows/active | High |r| pairs | Exact duplicates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 1 | 1 | 0 | 0 | n/a | n/a | 0 | 0 |
| NON_BTC_TRANSFER | 6 | 1 | 14 | 5 | 2.35 | 15.43 | 1 | 0 |

## Outcome-blind input integrity

- Status: `ALL_RETAINED_INPUTS_OBSERVED`
- Fully observed executable rows: 7/7
- Soft-degraded executable rows: 0
- Fully observed independent close windows: 1/1
- Soft-degraded independent close windows: 0
- Degradation by asset: {}
- Degradation by retained indicator: {}
- Exact-capture offset range: 0.06408262252807617 to 0.5347425937652588 seconds
- Evidence-assembly lag range: 0.05700874328613281 to 0.5225687026977539 seconds
- Timing status: `OK`
- Frozen eligibility/readiness credit changed: False

This is an outcome-blind diagnostic. It cannot change the pinned design, fit a model, emit an artifact, send a notification, trade, refit, or promote.
