---
name: updated-review
description: Run a comprehensive code-quality review of the tez Kalshi monitor and produce a 1-10 rating with concrete improvement ideas. Invoke when the user says "updated review", "update the review", "review my code", "rate my code", or asks for a fresh code/quality score of this repo.
---

# Updated Review

Produce a grounded, comprehensive code-quality review of this repository (a
read-only Kalshi 15-min crypto binary paper-trading monitor) and assign an
overall **1–10 rating for how well it does its job**, plus prioritized ideas to
raise the score.

This is a *read-only* review skill: **do not edit code, commit, or push.** The
deliverable is the written review in chat.

## How to run it

1. **Re-ground every time.** Don't reuse a past rating from memory — the code
   changes. Start fresh:
   - `git log --oneline -8` and `git status` to see what moved since last time.
   - `find . -name '*.py' -not -path './.git/*' | xargs wc -l | tail -1` for size.
   - List `tests/` and run `python3 -m pytest tests/ -q` (or at least collect:
     `python3 -m pytest tests/ -q --collect-only | tail -5`) to get the live
     test count and pass/fail state. **A failing suite caps the rating** — report
     it honestly with the failing output.

2. **Fan out the review across the subsystems in parallel** (use the `Explore`
   agent, one per area, in a single message). Each agent reads only its files,
   does not edit, and returns specific findings (with `file:line`), strengths,
   weaknesses, latent bugs, and a sub-rating 1–10. Cover:
   - **Decision engine / alerting** — `q15_upgrade/checkpoint_v95.py`,
     `q15_upgrade/window_focus.py`, `analysis.py`, `notifier.py`.
   - **Learning / calibration / persistence** — `q15_upgrade/ledger_v95.py`,
     `reporting.py`, `db.py`, `performance.py`.
   - **App loop / feeds / tests** — `app.py`, `spot_client.py`,
     `q15_upgrade/market_data_v95.py`, `cycle_watchdog.py`, and the `tests/`
     suite quality.
   - Skip the frozen legacy chain (`checkpoint_v91..v94*`) unless base behavior
     is in question — per `CLAUDE.md`.

3. **Judge against what the job actually is.** This is a money-trading edge tool.
   Weight the rating toward: statistical soundness (calibration, Wilson CIs,
   significance-tested promotion), correctness of the live prediction/alert path,
   robustness to feed failures (None/stale handling, fallbacks), the read-only
   invariant, and signal quality — not just generic code tidiness. Respect the
   invariants in `CLAUDE.md` (read-only, frozen champion weights, HTML/marker
   preservation).

## Output format

Deliver in chat, concise but specific:

- **Overall rating: X/10** — one-paragraph justification tied to the job.
- **Subsystem scores** — a small table (decision engine / learning / app+loop /
  tests), each with a one-line reason.
- **What's strong** — 3–6 bullets, with `file:line` where it helps.
- **What's holding the score back** — ranked weaknesses/latent bugs, `file:line`.
- **How to raise the rating** — prioritized, concrete steps grouped by impact
  (Highest / Medium / Polish), each saying which number it would move and roughly
  how much. Prefer changes that are default-OFF `Q15_*`-gated where they touch
  model behavior, and test-backed.
- **Delta since last review** — if a prior review is recoverable (git log /
  HANDOFF.md), note what changed and whether the score moved.

Keep it honest: if tests fail or a subsystem is weak, say so plainly. End with
the single highest-leverage next step.
