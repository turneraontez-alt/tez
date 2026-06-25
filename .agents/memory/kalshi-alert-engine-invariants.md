---
name: Kalshi alert-engine invariants
description: Rules any Telegram alert added to the Kalshi monitor must obey (read-only, claim-before-send, anti-spam).
---

Any alert feature added to the Kalshi monitor (checkpoint reports, dip alerts,
future alert types) lives in `q15_upgrade/window_focus.py` and must obey these
invariants. Verified against the dip-alert implementation.

**Rules:**
- **Read-only / observational.** An alert may read snapshots and mutate only its
  own isolated in-memory state cache (e.g. `self._dip_state`). It must NOT mutate
  engine/entry decisions and must NOT write to the learning corpus.
- **Single mutator.** Alerts fire from `_maybe_notify`, called inside the refresh
  loop via `TwoWindowFocusManager.update`. `refresh_loop` stays the sole engine
  mutator; do not send from other threads.
- **Claim before every send.** Always go through `_claim_and_send`, which calls
  `_claim` (→ `store.claim_event`, atomic `INSERT ... ON CONFLICT DO NOTHING` in
  `db.py`) BEFORE delivering. The events store AND the Telegram chat are SHARED
  across dev+prod processes, so an unclaimed send double-pings the owner.
- **Result handling:** `_claim_and_send` returns `sent` / `duplicate` / `failed`.
  On `failed` leave the trigger armed and counters untouched so the next cycle
  retries; on `duplicate`/`sent` advance local state so you don't starve on an
  already-claimed key. `_claim` fails CLOSED on store error (no local-fallback
  send).
- **Anti-spam:** one ping per event via hysteresis re-arm (re-arm only after the
  signal recedes below a rearm threshold), per-asset cooldown, per-cycle cap, and
  any quality floors (e.g. win-probability). All env-tunable with a kill switch
  that defaults OFF in production.
- **HTML-escape all free text** with `_esc` (`html.escape(..., quote=True)`)
  before wrapping in tags, and include the DISCLAIMER line.

**Why:** this is a live-money monitor sharing one DB + one Telegram chat between
dev and prod; an alert that mutates engine state, skips the claim, or lacks
hysteresis either corrupts trading behavior or spams/misleads the owner.
