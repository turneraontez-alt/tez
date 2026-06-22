#!/usr/bin/env bash
# reset_data.sh — start a CLEAN data baseline from this point forward.
#
# ARCHIVES (moves, never deletes) every collected data store into a timestamped
# old_data/reset_<UTC>/ folder, then leaves data/ empty so the app recreates
# fresh, empty databases on its next start. Everything old is preserved and fully
# recoverable — to roll back, move the files back out of old_data/.
#
# The live ledgers live on the Repl, so run this THERE (Shell), then Stop ▸ Run.
# Safe to run anywhere: if data/ is empty it archives nothing.
#
# Usage:
#   scripts/reset_data.sh            # archive all data/*.sqlite3* stores
#   DRY_RUN=1 scripts/reset_data.sh  # show what WOULD be archived, change nothing
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DATA_DIR="${Q15_DATA_DIR:-data}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
# Archive lands as a sibling of the data dir (./old_data when DATA_DIR=data).
ARCHIVE="$(dirname "$DATA_DIR")/old_data/reset_${TS}"
DRY_RUN="${DRY_RUN:-0}"

shopt -s nullglob
# Every SQLite store + its WAL/SHM sidecars + any published gzip copies.
files=(
  "${DATA_DIR}"/*.sqlite3 "${DATA_DIR}"/*.sqlite3-wal "${DATA_DIR}"/*.sqlite3-shm
  "${DATA_DIR}"/*.sqlite3.gz "${DATA_DIR}"/*.db
)

if [ ${#files[@]} -eq 0 ]; then
  echo "tez reset: no data files found in '${DATA_DIR}/' — nothing to archive."
  exit 0
fi

echo "tez data reset"
echo "  source : ${DATA_DIR}/"
echo "  archive: ${ARCHIVE}/   (move, NOT delete — fully recoverable)"
echo "  files  : ${#files[@]}"

if [ "$DRY_RUN" = "1" ]; then
  echo "  DRY_RUN=1 — would archive:"
  for f in "${files[@]}"; do echo "    $(basename "$f")"; done
  echo "  (nothing changed)"
  exit 0
fi

mkdir -p "$ARCHIVE"
{
  echo "tez data reset"
  echo "archived_at_utc: ${TS}"
  echo "git_commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "source_dir: ${DATA_DIR}"
  echo "files:"
} > "${ARCHIVE}/MANIFEST.txt"

for f in "${files[@]}"; do
  sz="$(stat -c%s "$f" 2>/dev/null || echo '?')"
  echo "  $(basename "$f")  (${sz} bytes)" >> "${ARCHIVE}/MANIFEST.txt"
  mv "$f" "${ARCHIVE}/"
done

echo "Archived ${#files[@]} file(s):"
cat "${ARCHIVE}/MANIFEST.txt"
echo
echo "Done. Fresh databases will be created on the next app start (Stop ▸ Run)."
echo "Rollback: move files back, e.g.  mv ${ARCHIVE}/*.sqlite3 ${DATA_DIR}/"
echo
echo "NOTE — Postgres 'signals' store is NOT touched by this script. If you also"
echo "want a clean Postgres baseline, archive that table separately, e.g.:"
echo "  ALTER TABLE signals RENAME TO signals_archived_${TS};"
echo "(the app recreates 'signals' on next start). Leave it as-is to keep history."
