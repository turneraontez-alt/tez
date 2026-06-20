# Q15 V9.5.2 Runtime Activation + Canonical Data Bridge

This hotfix addresses a live symptom where Telegram continued to show the
legacy `Three requirements` report after V9.5 was installed.

## What it fixes

- Audits and repairs the active runtime policy constructor so the worker uses
  `CheckpointPolicyV95` rather than a V9.2/V9.3/V9.4 policy.
- Forces the V9.5 runtime active by default unless the explicit emergency
  disable setting is enabled.
- Gives the legacy compatibility parent the same persistent candle cache used
  by V9.5.
- Derives 30s, 60s, and 180s spot momentum from that candle cache.
- Bridges fresh public exchange flow and order-book imbalance into compatible
  fields when local inputs are absent.
- Replaces legacy Q15 formatting with the unified V9.5 report.
- Preserves the separate 10M-primary shadow learner and read-only behavior.

## Install

```bash
python q15_v9_5_runtime_activation_data_bridge_hotfix_installer.py artifacts/kalshi-monitor
```

Restart or redeploy after installation. Stop any older Replit worker/deployment
first so it cannot continue sending legacy notifications.

## Expected Telegram header

```text
👀 10M V9.5 CHECK — BEST PICKS, NO ENTRY YET
```

The report must not contain `Three requirements` or a separate
`30M CHART CONTEXT` appendix.

## Verify

Open `/api/q15-v9-5/diagnostics` and confirm:

- `runtime_binding` is `CheckpointPolicyV95`
- `runtime_active` is true
- `cycles` is increasing
- `parent_input_bridge.assets_with_candles` is greater than zero
- `last_error` is null

This hotfix remains read-only and does not place, modify, or cancel orders.
