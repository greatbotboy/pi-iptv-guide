#!/usr/bin/env bash
# One-shot (needs sudo password once): install helper, tune boot, reboot.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing iptv-console helper + sudoers"
bash "$ROOT/deploy/install-console-helper.sh"

echo "==> Applying boot/OS tune"
sudo /usr/local/sbin/iptv-console tune-boot

echo "==> Optional: install mpv for DRM playback"
if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mpv; then
  mpv --version | head -1
else
  echo "mpv install skipped/failed — ffmpeg path remains"
fi

echo "==> Rebooting so config.txt takes effect"
sudo reboot
