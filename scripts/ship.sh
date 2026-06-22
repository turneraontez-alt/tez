#!/usr/bin/env bash
# One-command safe ship: branch -> main, with the data-safety guard built in.
#
#   scripts/ship.sh "short summary of what shipped"
#
# Steps (all idempotent, network ops retried):
#   1. fetch origin/main
#   2. merge origin/main INTO the work branch first  -> can only ADD main's
#      content, never delete it (the data-safety guard, made structural). A merge
#      CONFLICT aborts the ship for manual resolution.
#   3. run the full test suite  -> abort if red
#   4. stamp build_info.json (commit / time / summary / test count) + commit
#   5. fast-forward-merge the branch into main (--no-ff) and push main
#   6. push the work branch
# Leaves you back on the work branch. main and the branch end identical.
set -euo pipefail

SUMMARY="${1:-}"
if [ -z "$SUMMARY" ]; then
  echo "usage: scripts/ship.sh \"summary of what shipped\"" >&2
  exit 2
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "Refusing to ship from '$BRANCH' — run from your work branch." >&2
  exit 1
fi

retry() {  # retry <desc> <cmd...>
  local desc="$1"; shift
  local i
  for i in 1 2 3 4; do
    if "$@"; then return 0; fi
    echo "  $desc failed (attempt $i) — retrying in $((2**i))s" >&2
    sleep $((2**i))
  done
  echo "  $desc failed after retries" >&2
  return 1
}

echo "==> [1/6] fetch origin/main"
retry "fetch" git fetch origin main

echo "==> [2/6] preserve main's content (merge origin/main into $BRANCH)"
# Merging origin/main in can only ADD its commits/files to the branch; the later
# branch->main merge therefore cannot drop anything that exists on main. A
# conflict here means the SAME file changed both sides — stop and resolve.
if ! git merge origin/main --no-edit; then
  echo "MERGE CONFLICT with origin/main — ship aborted. Resolve, commit, re-run." >&2
  exit 1
fi

echo "==> [3/6] run test suite"
if ! TEST_OUT="$(python3 -m pytest tests/ -q 2>&1)"; then
  echo "$TEST_OUT" | tail -25
  echo "Tests are RED — ship aborted (nothing pushed)." >&2
  exit 1
fi
TEST_LINE="$(printf '%s\n' "$TEST_OUT" | tail -1)"
echo "  $TEST_LINE"

echo "==> [4/6] stamp build_info.json"
PRESHA="$(git rev-parse --short HEAD)"   # the code being shipped (pre-stamp)
NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$PRESHA" "$BRANCH" "$NOW_UTC" "$SUMMARY" "$TEST_LINE" <<'PY'
import json, sys
commit, branch, ts, summary, tests = sys.argv[1:6]
with open("build_info.json", "w", encoding="utf-8") as fh:
    json.dump({"commit": commit, "branch": branch, "committed_at": ts,
               "summary": summary, "tests": tests or None}, fh, indent=2)
    fh.write("\n")
PY
git add build_info.json
git commit -m "ship: $SUMMARY" >/dev/null 2>&1 || echo "  (build_info unchanged — nothing to commit)"

echo "==> [5/6] merge $BRANCH -> main (--no-ff) and push"
# Reset local main to the just-fetched/merged origin/main, so a stale or
# unrelated local 'main' can never derail the merge.
git branch -f main origin/main
git checkout main
git merge --no-ff "$BRANCH" -m "Merge $BRANCH: $SUMMARY"
retry "push main" git push origin main

echo "==> [6/6] back to $BRANCH and push"
git checkout "$BRANCH"
retry "push $BRANCH" git push -u origin "$BRANCH"

echo ""
echo "SHIPPED ✓  main is now $(git rev-parse --short origin/main)"
echo "  build : $PRESHA  ·  $NOW_UTC"
echo "  $SUMMARY"
echo "Verify on the Repl after Stop ▸ Run:  <your-repl-url>/version"
