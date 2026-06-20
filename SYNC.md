# Syncing the Replit project and GitHub `main`

Two mechanisms keep the Repl and GitHub `main` in sync. **The GitHub Relay is
the normal one and runs automatically — you rarely touch anything.** `./sync.sh`
is now only a manual recovery override.

## 1. The GitHub Relay (automatic, two-way) — the normal path

A background workflow (`tools/github_relay.py`, started in parallel with the app
by `.replit`) syncs **both directions every ~20s**:

- **GitHub → Repl:** new commits on `main` (e.g. a merged PR) are *merged* down
  into the Repl automatically.
- **Repl → GitHub:** anything committed locally on the Repl is pushed up to
  `main`.

It uses a real `git merge` (never a destructive reset, never a force-push). If
the **same file** changed on both sides it can't auto-resolve, so it **pauses
and leaves the project untouched**, logging the conflicting files in the
*GitHub Relay* console — resolve those and it resumes.

Requirements: a `GH_PUSH_TOKEN` (or `GITHUB_TOKEN`) Secret with **Contents:
Read+Write**. Without it the relay logs "cannot run" and does nothing.

**What this means day to day:**
- After a PR is merged, the Repl picks up the new code within ~20s — **no manual
  sync needed.** You still **Stop ▸ Run** to load it into the running app
  (`python3 app.py` does not hot-reload).
- ⚠️ The relay pushes **anything committed on the Repl straight to `main`,
  bypassing pull-request review.** Be deliberate about what you commit on the
  Repl. For model/behaviour changes prefer a branch + PR — the champion weights
  are FROZEN and promotion is meant to be manual and significance-tested.

## 2. `./sync.sh` — manual override (force the Repl to match GitHub)

Use this **only** to force the Repl to exactly match GitHub `main` — e.g. to
recover when the relay is paused on a conflict and you want **GitHub to win**:

```bash
./sync.sh            # public fetch
./sync.sh <TOKEN>    # private repo: fine-grained PAT, Contents: Read
BRANCH=some-branch ./sync.sh   # match a non-main branch
```

⚠️ **Destructive:** it `git reset --hard`s tracked files to GitHub, discarding
any local-only commits the relay has not pushed yet. Your `.env`, Replit
Secrets, and `data/` are gitignored and untouched. After it runs: **Stop ▸ Run**.

> To resolve a relay conflict by *merging* both sides instead of discarding
> local work, run `python3 tools/github_reconcile.py` (one-shot merge + push).
>
> First time only, if the shell says `permission denied`: `chmod +x sync.sh`.

## Token note

Both the relay and `sync.sh` authenticate with a GitHub token read from the
environment and injected into the fetch/push URL **in memory only** — never
written to `.git/config`, never committed, masked in logs. For the relay set
`GH_PUSH_TOKEN` (Contents: Read+Write); for a private-repo `sync.sh` pull,
Contents: Read is enough.
