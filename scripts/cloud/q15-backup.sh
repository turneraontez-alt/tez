#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${Q15_APP_DIR:-/opt/q15/app}"
PYTHON="${Q15_PYTHON:-/opt/q15/venv/bin/python}"
BACKUP_DIR="${Q15_BACKUP_DIR:-/var/backups/q15}"
KEEP="${Q15_BACKUP_KEEP:-7}"
LOCK_FILE="${Q15_BACKUP_LOCK:-/run/lock/q15-backup.lock}"

mkdir -p "${BACKUP_DIR}"
args=("${APP_DIR}/tools/local_backup.py" --repo "${APP_DIR}" --destination "${BACKUP_DIR}")
if [[ "${Q15_BACKUP_INCLUDE_HIGH_VOLUME:-false}" == "true" ]]; then
  args+=(--include-high-volume)
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "another Q15 backup is already running" >&2
  exit 0
fi

"${PYTHON}" "${args[@]}"
mapfile -t backups < <(find "${BACKUP_DIR}" -maxdepth 1 -type f \
  -name 'q15-data-*.zip' -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
for ((i=KEEP; i<${#backups[@]}; i++)); do
  rm -f -- "${backups[$i]}"
done
