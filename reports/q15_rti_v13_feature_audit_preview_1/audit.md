# Q15 RTI microstructure numerical feature audit

- Status: `PREVIEW_ONLY_BEFORE_30_WINDOWS`
- Design: `q15-rti-market-residual-cohort-conditioned-compact-v13`
- Fingerprint: `adc900b5882567446cb3d4a8f5fc0cb795e278dd38db2c6179e54cc83fc673ed`
- Executable seven-asset windows: 1
- Windows remaining to first review: 29
- Outcome labels read: False
- Model fit: False
- Feature profile allow-list enforced: True
- Raw threshold JSON selected by CLI loader: False

| Cohort | Rows | Windows | Active | Rank | Stable rank | Projected train rows/active | High |r| pairs | Exact duplicates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 1 | 1 | 0 | 0 | n/a | n/a | 0 | 0 |
| NON_BTC_TRANSFER | 6 | 1 | 12 | 5 | 2.04 | 18.00 | 4 | 0 |

This is an outcome-blind diagnostic. It cannot change the pinned design, fit a model, emit an artifact, send a notification, trade, refit, or promote.

## Frozen V13 geometry review

- Protocol: `q15-rti-v13-outcome-blind-geometry-review-v1`
- Protocol SHA-256: `550e8dfd3132712020aa90232dab97679cf209a8d5ba5438c6bd7d786b42605b`
- Status: `WAITING_FOR_30_COMPLETE_WINDOWS`
- Review ready: False
- BTC conditioned feature maximum absolute value: 0.0
- All frozen checks met: False
- Outcome labels read: False
- Automatic design change: False

## Frozen V13 chronological covariate-drift review

- Protocol: `q15-rti-v13-outcome-blind-60-window-covariate-drift-v1`
- Protocol SHA-256: `91589996d48ec047b74b5e8c25c4b92533b220dd4a41729cbc71f91aa14a5856`
- Status: `WAITING_FOR_60_COMPLETE_WINDOWS`
- Review ready: False
- Same-close assets share a half: True
- Preview breach count: 42
- Confirmed drift detected: False
- Outcome labels read: False
- Automatic feature or threshold changes: False
