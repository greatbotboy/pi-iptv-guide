#!/usr/bin/env bash
# Run on the Pi (needs sudo): installs VLC system-wide for HDMI playback.
set -euo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y vlc
cvlc --version | head -1
echo "VLC installed. Play from the guide should work on DISPLAY=:0"
