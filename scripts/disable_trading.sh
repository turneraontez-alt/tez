#!/usr/bin/env bash
# disable_trading.sh — instantly HALT all live executor entries (NO book + YES bot).
#
# Creates a filesystem kill-file that the executor checks on EVERY fire, so the halt
# takes effect on the next ~1s cycle — NO restart, NO Secrets edit. It blocks new
# ENTRIES only; defensive EXITS still run, so an already-open position can be closed
# while trading is halted. (For a TOTAL freeze incl. exits, set Q15_EXEC_KILL=true in
# the Repl Secrets and restart — see the note at the end.)
#
# Run this ON THE REPL (Shell), where the live executor and its data/ dir live.
#
# Usage:
#   scripts/disable_trading.sh                 # HALT entries now (create the kill-file)
#   scripts/disable_trading.sh "reason text"   # HALT, recording a reason in the file
#   scripts/disable_trading.sh --status        # show whether trading is halted
#   scripts/disable_trading.sh --enable        # RESUME trading (remove the kill-file)
#
# The kill-file path is data/EXECUTOR_DISABLED, or $Q15_EXEC_DISABLE_FILE if set (it
# MUST match what the running app reads — set the same env in both places, or rely on
# the default). Both bots share one file, so this halts/resumes them together.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KILL_FILE="${Q15_EXEC_DISABLE_FILE:-data/EXECUTOR_DISABLED}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

case "${1:-}" in
  --status|-s|status)
    if [ -e "$KILL_FILE" ]; then
      echo "TRADING HALTED — kill-file present: $KILL_FILE"
      echo "----"
      cat "$KILL_FILE" 2>/dev/null || true
      echo "----"
      echo "Resume with:  scripts/disable_trading.sh --enable"
    else
      echo "TRADING ENABLED — no kill-file at $KILL_FILE"
      echo "Halt with:    scripts/disable_trading.sh"
    fi
    exit 0
    ;;
  --enable|--on|--resume|enable)
    if [ -e "$KILL_FILE" ]; then
      rm -f "$KILL_FILE"
      echo "Trading RESUMED — removed $KILL_FILE"
      echo "New entries will be admitted again on the next ~1s cycle."
    else
      echo "Already enabled — no kill-file at $KILL_FILE (nothing to do)."
    fi
    exit 0
    ;;
  --help|-h|help)
    sed -n '2,28p' "${BASH_SOURCE[0]}"
    exit 0
    ;;
esac

REASON="${1:-manual disable via scripts/disable_trading.sh}"
mkdir -p "$(dirname "$KILL_FILE")"
{
  echo "EXECUTOR TRADING DISABLED"
  echo "disabled_at_utc: ${TS}"
  echo "by: ${USER:-unknown}@$(hostname 2>/dev/null || echo host)"
  echo "git_commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "reason: ${REASON}"
} > "$KILL_FILE"

echo "TRADING HALTED."
echo "  kill-file: $KILL_FILE"
echo "  effect   : the executor refuses ALL new entries (NO book + YES bot) within ~1s."
echo "             Defensive EXITS still run — open positions can still be closed."
echo
cat "$KILL_FILE"
echo
echo "Resume with:  scripts/disable_trading.sh --enable"
echo
echo "NOTE — this halts ENTRIES, not the whole process. For a TOTAL freeze (entries AND"
echo "exits), set Q15_EXEC_KILL=true in the Repl Secrets and restart (Stop ▸ Run); to turn"
echo "the executor off entirely, set Q15_EXEC_ENABLED=false (and Q15_EXEC_YES_ENABLED=false)."
echo "To CLOSE open positions now, use:  python3 scripts/exec_preflight.py --flatten <ticker>"
