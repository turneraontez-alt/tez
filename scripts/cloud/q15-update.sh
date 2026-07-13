#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "q15-update must run as root" >&2
  exit 1
fi

APP_DIR="${Q15_APP_DIR:-/opt/q15/app}"
VENV_DIR="${Q15_VENV_DIR:-/opt/q15/venv}"
REF="${Q15_REPO_REF:-main}"

git -C "${APP_DIR}" fetch origin "${REF}"
git -C "${APP_DIR}" checkout "${REF}"
git -C "${APP_DIR}" merge --ff-only "origin/${REF}"
"${VENV_DIR}/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"
bash "${APP_DIR}/scripts/cloud/install-q15.sh"
systemctl restart q15.service
/usr/local/sbin/q15-healthcheck
