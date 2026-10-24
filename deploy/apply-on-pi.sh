#!/usr/bin/env bash
# Apply the current checkout on the Pi. Used by the self-hosted Actions runner.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${IPTV_DIR:-/home/pi/iptv-guide}"

if [[ ! -f "${ROOT}/requirements.txt" || ! -f "${ROOT}/deploy/iptv-guide.service" ]]; then
  echo "Not an iptv-guide checkout: ${ROOT}" >&2
  exit 1
fi

mkdir -p "${DEST}"
if [[ "$(cd "${ROOT}" && pwd -P)" == "$(cd "${DEST}" && pwd -P)" ]]; then
  echo "Refusing to sync ${ROOT} onto itself" >&2
  exit 1
fi

echo "==> Syncing ${ROOT} -> ${DEST}"
rsync -a --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude 'data/channels.json' \
  --exclude 'data/epg.json' \
  --exclude 'data/local.m3u' \
  --exclude 'app/config.local.yaml' \
  --exclude 'bin/ffmpeg' \
  --exclude 'bin/ffprobe' \
  --exclude 'bin/ffmpeg-lib/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '*.pem' \
  --exclude 'id_rsa' \
  --exclude 'id_ed25519' \
  "${ROOT}/" "${DEST}/"

echo "==> Installing Python dependencies"
python3 -m venv "${DEST}/.venv"
"${DEST}/.venv/bin/pip" install -q -r "${DEST}/requirements.txt"

echo "==> Restarting iptv-guide"
if sudo -n true 2>/dev/null; then
  pi_uid="$(id -u)"
  sed "s|XDG_RUNTIME_DIR=/run/user/[0-9]*|XDG_RUNTIME_DIR=/run/user/${pi_uid}|" \
    "${DEST}/deploy/iptv-guide.service" | sudo tee /etc/systemd/system/iptv-guide.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable iptv-guide.service
  sudo systemctl restart iptv-guide.service
else
  mkdir -p "${HOME}/.config/systemd/user"
  cp "${DEST}/deploy/iptv-guide.user.service" "${HOME}/.config/systemd/user/iptv-guide.service"
  systemctl --user daemon-reload
  systemctl --user enable iptv-guide.service
  systemctl --user restart iptv-guide.service
fi

echo "==> Waiting for the guide"
for _ in $(seq 1 45); do
  if curl -sf http://127.0.0.1:8080/api/status; then
    echo
    exit 0
  fi
  sleep 1
done

echo "Guide did not become ready" >&2
if sudo -n true 2>/dev/null; then
  sudo journalctl -u iptv-guide -n 40 --no-pager || true
else
  journalctl --user -u iptv-guide -n 40 --no-pager || true
fi
exit 1
