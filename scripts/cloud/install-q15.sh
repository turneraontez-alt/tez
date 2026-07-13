#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "install-q15.sh must run as root" >&2
  exit 1
fi

REPO_URL="${Q15_REPO_URL:-https://github.com/turneraontez-alt/tez.git}"
REPO_REF="${Q15_REPO_REF:-main}"
Q15_ROOT="${Q15_ROOT:-/opt/q15}"
APP_DIR="${Q15_APP_DIR:-${Q15_ROOT}/app}"
VENV_DIR="${Q15_VENV_DIR:-${Q15_ROOT}/venv}"
STATE_DIR="${Q15_STATE_DIR:-/var/lib/q15}"
CONFIG_DIR="${Q15_CONFIG_DIR:-/etc/q15}"
BACKUP_DIR="${Q15_BACKUP_DIR:-/var/backups/q15}"
SERVICE_USER="${Q15_SERVICE_USER:-q15}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git python3 python3-pip python3-venv sqlite3 ufw util-linux

ufw allow OpenSSH
ufw --force enable

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${STATE_DIR}" \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0755 "${Q15_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 \
  "${STATE_DIR}" "${STATE_DIR}/data" "${STATE_DIR}/work" "${BACKUP_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  git clone --branch "${REPO_REF}" --single-branch "${REPO_URL}" "${APP_DIR}"
else
  git -C "${APP_DIR}" remote set-url origin "${REPO_URL}"
  git -C "${APP_DIR}" fetch origin "${REPO_REF}"
  git -C "${APP_DIR}" checkout "${REPO_REF}"
  git -C "${APP_DIR}" merge --ff-only "origin/${REPO_REF}"
fi

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

link_state_path() {
  local target=$1
  local link=$2
  if [[ -L "${link}" ]]; then
    ln -sfn "${target}" "${link}"
    return
  fi
  if [[ -e "${link}" ]]; then
    if [[ -d "${link}" ]] && [[ -z $(find "${link}" -mindepth 1 -print -quit) ]]; then
      rmdir "${link}"
    else
      echo "Refusing to replace non-empty state path: ${link}" >&2
      exit 1
    fi
  fi
  ln -s "${target}" "${link}"
}

link_state_path "${STATE_DIR}/data" "${APP_DIR}/data"
link_state_path "${STATE_DIR}/work" "${APP_DIR}/work"
link_state_path "${STATE_DIR}/q15_v91_state.sqlite3" \
  "${APP_DIR}/q15_v91_state.sqlite3"

if [[ ! -f "${CONFIG_DIR}/q15.env" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${APP_DIR}/scripts/cloud/q15.env.example" "${CONFIG_DIR}/q15.env"
fi
if [[ ! -f "${CONFIG_DIR}/safety.env" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${APP_DIR}/scripts/cloud/q15-safety.env" "${CONFIG_DIR}/safety.env"
fi

install -m 0755 "${APP_DIR}/scripts/cloud/q15-healthcheck.sh" \
  /usr/local/sbin/q15-healthcheck
install -m 0755 "${APP_DIR}/scripts/cloud/q15-backup.sh" \
  /usr/local/sbin/q15-backup
install -m 0755 "${APP_DIR}/scripts/cloud/q15-update.sh" \
  /usr/local/sbin/q15-update
install -m 0755 "${APP_DIR}/tools/cloud_restore.py" \
  /usr/local/sbin/q15-restore
install -m 0644 "${APP_DIR}/scripts/cloud/systemd/"*.service \
  "${APP_DIR}/scripts/cloud/systemd/"*.timer /etc/systemd/system/

chown -R root:"${SERVICE_USER}" "${APP_DIR}" "${VENV_DIR}"
chmod -R g+rX,o-rwx "${APP_DIR}" "${VENV_DIR}"
systemctl daemon-reload
systemctl enable q15.service q15-healthcheck.timer q15-backup.timer

echo "Q15 cloud runtime installed but not started."
echo "Next: install /etc/q15/q15.env, restore state, then start q15.service and timers."
