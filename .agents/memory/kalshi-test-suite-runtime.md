---
name: Kalshi full pytest runtime
description: Why the full pytest suite can't be run inline in this env, and what to run instead.
---

The full `python3.11 -m pytest` suite (~1133 tests) is fast on CI/build machines
(~40s) but is **very slow in the Replit dev container** — each test pays a
per-test Postgres setup cost, so the whole suite runs well past 10 minutes
serially and blows the 120s tool timeout.

**Why this trips you up:** `pytest -q` buffers stdout when not attached to a tty,
so a backgrounded run shows a 0-byte log with no visible progress — it looks hung
when it is merely slow.

**How to apply:**
- Don't try to run the entire suite inline expecting a result inside 120s.
- Validate the surface you actually touched, e.g.
  `python3.11 -m pytest tests/test_q15_dip_alert.py tests/test_app_refresh_loop.py tests/test_q15_alert_send_retry.py -q`
  (these ran in ~35s and cover the alert/refresh-loop path).
- Always invoke pytest with `python3.11` — bare `python3` is the broken 3.12.
- Backgrounded long runs must be `setsid`-detached; a plain `&` job is killed when
  the bash tool's shell exits between calls.
