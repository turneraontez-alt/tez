# Syncing Replit to GitHub (`main` is the source of truth)

The site runs on Replit, but **GitHub `main` is the single source of truth**.
Use these steps to make the Repl exactly match GitHub. Run them in the Replit
**Shell**.

## One-time note on auth
This Repl has no `origin` remote pointing at GitHub, so we fetch the URL
directly. If the repo is **private**, create a token first:
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained →
repo access to `tez`, permission **Contents: Read** → generate → copy it.

## Sync steps

```bash
# 1) Fetch GitHub's main directly (no permanent remote; token not stored in config).
git fetch https://github.com/turneraontez-alt/tez.git main
#    If it errors asking for credentials (private repo), use the token:
#    git fetch https://YOUR_TOKEN@github.com/turneraontez-alt/tez.git main

# 2) Make every TRACKED file exactly match GitHub
#    (removes deleted files, adds new ones, updates the rest).
git reset --hard FETCH_HEAD

# 3) Reinstall dependencies, then restart the app (Stop ▸ Run).
pip install -r requirements.txt

# 4) Verify — the latest GitHub commit should be at the top.
git log -1
```

## Safety

- `reset --hard` only moves **tracked** files. Your `.env`, Replit **Secrets**,
  and the `data/` folder (SQLite/runtime state) are gitignored and untouched.
- **Do not run `git clean -fdx`** — the `-x` would delete gitignored files,
  including your secrets and database. Plain `reset --hard` is enough; you do not
  need `clean` to match GitHub's code.
- The previous Replit state is auto-saved by Replit's `gitsafe-backup`, so an
  overwrite is recoverable.

## Avoiding re-divergence (important)

The Replit Agent makes its own commits when it edits code, which silently
diverges the Repl from GitHub. To keep `main` authoritative, either:

- use the Replit Agent **only to run** the app, not to edit code, **or**
- if the Agent makes a change you want to keep, **push it to GitHub** so both
  stay in sync.

Otherwise you will have to re-run the overwrite above every time.
