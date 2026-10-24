#!/usr/bin/env bash
# Run on the Pi (needs sudo): seatd + cage + Chromium for the idle HDMI guide.
# Direct cog DRM paints a frame then snows/crashes on Raspberry Pi OS.
set -euo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y seatd cage chromium
sudo systemctl enable --now seatd.service
# seatd listens as group video (see ExecStart=seatd -g video)
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx video; then
  sudo usermod -aG video "$USER"
  echo "Added $USER to video — log out/in (or reboot) if cage still cannot open the seat."
fi
systemctl is-active seatd
cage -h 2>&1 | head -3 || true
chromium --version | head -1
echo "Idle guide will fill HDMI while no channel is playing (cage + Chromium)."
