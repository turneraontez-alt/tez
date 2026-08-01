# Q15 RTI microstructure numerical feature audit

- Status: `PREVIEW_ONLY_BEFORE_30_WINDOWS`
- Design: `q15-rti-market-residual-cohort-conditioned-compact-v13`
- Fingerprint: `adc900b5882567446cb3d4a8f5fc0cb795e278dd38db2c6179e54cc83fc673ed`
- Executable seven-asset windows: 5
- Windows remaining to first review: 25
- Outcome labels read: False
- Model fit: False
- Feature profile allow-list enforced: True
- Raw threshold JSON selected by CLI loader: False

| Cohort | Rows | Windows | Active | Rank | Stable rank | Projected train rows/active | High |r| pairs | Exact duplicates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 5 | 5 | 17 | 4 | 1.89 | 5.29 | 8 | 0 |
| NON_BTC_TRANSFER | 30 | 5 | 20 | 20 | 2.60 | 10.80 | 2 | 0 |

## Outcome-blind input integrity

- Status: `SOFT_DEGRADATION_PRESENT`
- Fully observed executable rows: 34/35
- Soft-degraded executable rows: 1
- Fully observed independent close windows: 4/5
- Soft-degraded independent close windows: 1
- Degradation by asset: {"BNB": 1}
- Degradation by retained indicator: {"spot_flow_missing": 1}
- Exact-capture offset range: 0.0014214515686035156 to 0.4664041996002197 seconds
- Evidence-assembly lag range: 0.021198749542236328 to 0.4436459541320801 seconds
- Timing status: `OK`
- Frozen eligibility/readiness credit changed: False

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
- Preview breach count: 10
- Confirmed drift detected: False
- Outcome labels read: False
- Automatic feature or threshold changes: False
