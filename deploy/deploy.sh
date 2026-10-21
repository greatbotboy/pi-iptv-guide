#!/usr/bin/env bash
set -euo pipefail

HOST="${IPTV_HOST:-}"
REMOTE_DIR="${IPTV_REMOTE_DIR:-/home/pi/iptv-guide}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "${HOST}" ]]; then
  echo "Set IPTV_HOST to the Pi SSH target, for example:" >&2
  echo "  IPTV_HOST=pi@raspberrypi.local $0" >&2
  exit 1
fi

GUIDE_HOST="${HOST#*@}"

echo "==> Syncing to ${HOST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'data/channels.json' \
  --exclude 'data/epg.json' \
  --exclude 'bin/' \
  "${ROOT}/" "${HOST}:${REMOTE_DIR}/"

echo "==> Configuring on Pi"
ssh "${HOST}" bash -s -- "${GUIDE_HOST}" <<'EOF'
set -euo pipefail
GUIDE_HOST="$1"
REMOTE_DIR="$HOME/iptv-guide"
cd "$REMOTE_DIR"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p ~/.config/systemd/user
cp deploy/iptv-guide.user.service ~/.config/systemd/user/iptv-guide.service
systemctl --user daemon-reload
systemctl --user enable iptv-guide.service
systemctl --user restart iptv-guide.service

# Prefer system-wide unit + VLC when passwordless sudo is available
if sudo -n true 2>/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq vlc
  PI_UID="$(id -u)"
  sed "s|XDG_RUNTIME_DIR=/run/user/[0-9]*|XDG_RUNTIME_DIR=/run/user/${PI_UID}|" \
    deploy/iptv-guide.service | sudo tee /etc/systemd/system/iptv-guide.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now iptv-guide.service
  systemctl --user stop iptv-guide.service || true
  systemctl --user disable iptv-guide.service || true
  bash "$REMOTE_DIR/deploy/install-console-helper.sh"
  echo "Installed system service + VLC + console helper"
else
  echo "NOTE: sudo unavailable — running as user systemd service."
  if command -v cvlc >/dev/null 2>&1; then
    echo "VLC available"
  elif [[ -x "$REMOTE_DIR/bin/ffmpeg" ]]; then
    echo "VLC missing — using bundled ffmpeg framebuffer playback"
    echo "Optional: sudo apt-get install -y vlc"
  else
    echo "WARNING: no VLC or ffmpeg — install with: sudo apt-get install -y vlc"
  fi
  echo "To hide the login cursor during play, run once on the Pi:"
  echo "  sudo bash $REMOTE_DIR/deploy/install-console-helper.sh"
fi


# Ensure the guide process is running (system unit preferred; user unit fallback)
if sudo -n systemctl restart iptv-guide.service 2>/dev/null; then
  systemctl --user stop iptv-guide.service 2>/dev/null || true
elif systemctl --user restart iptv-guide.service 2>/dev/null; then
  :
else
  pkill -u "$(id -u)" -f 'uvicorn main:app' 2>/dev/null || true
  sleep 1
  systemctl --user start iptv-guide.service 2>/dev/null || true
fi

sleep 3
systemctl is-active iptv-guide.service 2>/dev/null || systemctl --user is-active iptv-guide.service || true
curl -sf http://127.0.0.1:8080/api/status || curl -sf http://127.0.0.1:8080/api/status
echo
echo "Guide: http://${GUIDE_HOST}:8080"
EOF
