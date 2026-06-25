---
name: Python interpreter must be pinned to 3.11
description: Why the Kalshi app workflow/deploy must run python3.11 explicitly, not bare python3.
---

The project's intended interpreter is python-3.11 (`.replit modules`), and only 3.11
has working C-extension backends: `_cffi_backend` (so `cryptography` signing
primitives — serialization/hashes — actually work) and `psycopg2._psycopg`.

**The trap:** bare `python3` can resolve to a nix-provided **3.12** instead. Unused
nix packages (`executor`, which pulls `coloredlogs` + `humanfriendly`, all
`python3.12-*`) land their 3.12 bin dir *first* in PATH and shadow the 3.11 module.
On 3.12: `psycopg2._psycopg` and `_cffi_backend` are missing, so the app boots
DEGRADED — "psycopg2 unavailable; signal persistence disabled" and "WebSocket
disabled: install websockets and cryptography", and the Kalshi signer cannot sign
live orders (shallow `import cryptography` still succeeds, masking it).

**Why:** this silently breaks live order signing with real money while the app still
serves 200s and looks healthy.

**Required guard mechanism (keep both layers):**
- Dev workflow "Start application" command must be `python3.11 app.py`.
- `app.py` must keep a top-of-file interpreter guard (before any heavy/crypto/db
  import) that re-execs under `python3.11`, using a `Q15_INTERP_REEXEC=1` sentinel
  to avoid loops, and **fails closed** — if 3.11 is unreachable it exits non-zero
  rather than continuing on 3.12. Needed because the prod gce deploy launches via
  bare `python3` and `[deployment].run` is not agent-editable. Never let it fail
  open: a degraded 3.12 boot still serves 200s but cannot sign, which is the
  dangerous case for real money. Re-publish after changes for the guard to reach
  production.

**How to apply:**
- Run the app as `python3.11 app.py` (workflow command), not `python3 app.py`.
- Healthy-boot signature: log line `SignalStore connected to Postgres` and NO
  psycopg2/WebSocket-disabled warnings.
- Verify the live interpreter: `readlink -f /proc/$(pgrep -f "[a]pp.py")/exe` must be
  a `python3-3.11.x` path.
- The deployment run command (`.replit [deployment].run = ["python3","app.py"]`) has
  the same hazard in production — it should also use python3.11.
- A bash shell's bare `python3` is also 3.12, so run preflight/scripts with the
  3.11 binary (`command -v python3.11`) or imports fail with `_cffi_backend`.
