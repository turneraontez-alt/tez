#!/usr/bin/env bash
set -Eeuo pipefail

HEALTH_URL="${Q15_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
STATE_PATH="${Q15_STATE_DIR:-/var/lib/q15}"
MAX_DISK_PERCENT="${Q15_MAX_DISK_PERCENT:-85}"

healthy() {
  curl --fail --silent --show-error --max-time 15 "${HEALTH_URL}" \
    | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ok" else 1)'
}

if ! systemctl is-active --quiet q15.service || ! healthy; then
  logger -t q15-healthcheck "health failed; restarting q15.service"
  systemctl restart q15.service
  sleep 15
  if ! healthy; then
    logger -t q15-healthcheck "health still failed after restart"
    exit 1
  fi
fi

disk_percent=$(df -P "${STATE_PATH}" | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')
if [[ ! "${disk_percent}" =~ ^[0-9]+$ ]]; then
  logger -t q15-healthcheck "could not determine disk use for ${STATE_PATH}"
  exit 1
fi
if (( disk_percent >= MAX_DISK_PERCENT )); then
  logger -t q15-healthcheck \
    "disk use is ${disk_percent}% at ${STATE_PATH}; limit is ${MAX_DISK_PERCENT}%"
  exit 2
fi
