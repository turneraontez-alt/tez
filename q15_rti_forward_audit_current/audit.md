# Q15 RTI Frozen-vs-Forward Drift Audit

Mode: **paper-only; descriptive; no threshold promotion**

| Period | n | Accuracy | Wilson 95% | 10-lot net | EV/contract | Trades/window |
|---|---:|---:|---:|---:|---:|---:|
| frozen_historical | 10 | 70.0% | 39.7%-89.2% | $9.49 | 9.49c | 0.213 |
| post_freeze_forward | 100 | 48.0% | 38.5%-57.7% | $-141.93 | -14.19c | 0.472 |

## Honest conclusion

The frozen sample was 7/10; the forward sample is 48/100. Two-sided Fisher exact p=0.3203.  The large observed drop is economically real, but the original sample was too small to prove that a stable edge existed or to identify a statistically secure regime break.

The forward period has now been inspected.  It can diagnose failure but cannot be reused as an untouched test or promote a newly selected filter. Any new rule must freeze first and earn evidence on later durable rows.

## Forward diagnostics (not selection evidence)

- >=1 bps 61-second momentum: 26/65, $-146.98.
- Fresh spot-book aligned: 26/46, $-27.87.
- BTC and non-BTC transfer cohorts are both below fee+slippage break-even.
- Enhanced path features were unavailable for the original frozen sample (0/10 rows), so their apparent forward relationships cannot be called a measured regime change.

## Integrity

- Valid exact rows: 1759
- Rejected rows: 73
- Exact timestamp, complete 61-second path, fresh quote, coherent grading, and canonical Kalshi fee + 2c slippage checks are mandatory.
- All assets sharing a close remain in the same frozen/forward period.
