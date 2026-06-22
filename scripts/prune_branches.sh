#!/usr/bin/env bash
# Prune stale claude/* branches so the repo keeps ONE branch (main).
#
#   scripts/prune_branches.sh            # dry run: list what WOULD be deleted
#   scripts/prune_branches.sh --merged   # delete only branches fully merged into main
#   scripts/prune_branches.sh --all      # delete ALL claude/* (merged + unmerged)
#
# Always keeps `main` and the branch currently checked out. Run this on the Repl
# (or anywhere your GitHub token has Contents: write / delete-ref permission) —
# the web sandbox's relay proxy refuses ref deletion (HTTP 403).
set -euo pipefail

MODE="${1:---dry}"
CURRENT="$(git rev-parse --abbrev-ref HEAD)"
git fetch --prune origin >/dev/null 2>&1 || true

mapfile -t ALL < <(git branch -r | sed 's/^ *//' | grep '^origin/claude/' | sed 's#^origin/##')

to_delete=()
for b in "${ALL[@]}"; do
  [ "$b" = "$CURRENT" ] && continue            # never delete the checked-out branch
  if git merge-base --is-ancestor "origin/$b" origin/main 2>/dev/null; then
    state="merged"
  else
    state="unmerged"
  fi
  case "$MODE" in
    --merged) [ "$state" = "merged" ] && to_delete+=("$b") ;;
    --all)    to_delete+=("$b") ;;
    *)        echo "[$state] $b" ;;
  esac
done

if [ "$MODE" = "--dry" ] || [ "$MODE" = "-n" ]; then
  echo ""
  echo "Dry run. Re-run with --merged (safe) or --all (everything) to delete."
  exit 0
fi

if [ "${#to_delete[@]}" -eq 0 ]; then
  echo "Nothing to delete."
  exit 0
fi

echo "Deleting ${#to_delete[@]} branch(es): ${to_delete[*]}"
# git supports deleting many refs in one push.
git push origin --delete "${to_delete[@]}"
echo "Done. Remaining branches:"
git branch -r | sed 's/^ *//'
