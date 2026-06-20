#!/usr/bin/env bash
#
# sync.sh — Make this Replit exactly match GitHub (the single source of truth),
# WITHOUT deleting your secrets or runtime data.
#
# Run it in the Replit **Shell**:
#
#   ./sync.sh                  # public repo, or token already in env / Replit Secret
#   ./sync.sh <TOKEN>          # private repo: pass a fine-grained PAT (Contents: Read)
#   GITHUB_TOKEN=ghp_xxx ./sync.sh
#   BRANCH=some-branch ./sync.sh   # sync a different branch (default: main)
#
# What it does:
#   1. Fetches GitHub's branch directly (no permanent remote; token not stored).
#   2. Hard-resets TRACKED files to match GitHub (adds new, updates changed,
#      removes files deleted upstream).
#   3. Reinstalls Python deps.
#   4. Prints the new HEAD so you can verify.
#
# What it NEVER touches — all gitignored, so `reset --hard` leaves them alone:
#   .env, Replit Secrets, data/ (SQLite + runtime state), logs, backups.
#
# After it finishes, restart the app: Stop ▸ Run (or republish the deployment).

set -euo pipefail

REPO="github.com/turneraontez-alt/tez.git"
BRANCH="${BRANCH:-main}"
# Token precedence: first CLI arg, then $GITHUB_TOKEN, else none (public fetch).
TOKEN="${1:-${GITHUB_TOKEN:-}}"

if [[ -n "$TOKEN" ]]; then
  FETCH_URL="https://${TOKEN}@${REPO}"
else
  FETCH_URL="https://${REPO}"
fi

echo "==> Fetching '${BRANCH}' from GitHub..."
# Note: do not enable 'set -x' — it would print the token embedded in the URL.
git fetch "$FETCH_URL" "$BRANCH"

echo "==> Current commit: $(git rev-parse --short HEAD 2>/dev/null || echo none)"

# Warn before overwriting uncommitted edits to TRACKED files. Untracked and
# gitignored files (.env, data/, logs) are never affected by reset --hard.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "!!  You have uncommitted changes to tracked files; they will be OVERWRITTEN."
  echo "!!  (Gitignored files — .env, Replit Secrets, data/ — are safe.)"
  if [[ -t 0 ]]; then
    read -r -p "    Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted — nothing changed."; exit 1; }
  elif [[ "${FORCE:-}" != "1" ]]; then
    echo "    Non-interactive shell: re-run with FORCE=1 to proceed. Aborted."
    exit 1
  fi
fi

echo "==> Resetting tracked files to GitHub '${BRANCH}'..."
git reset --hard FETCH_HEAD

echo "==> Installing dependencies..."
pip install -r requirements.txt

echo "==> Done. Now at:"
git log -1 --oneline
echo
echo "Restart the app: Stop ▸ Run (or republish the deployment)."
