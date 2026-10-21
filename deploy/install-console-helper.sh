#!/usr/bin/env bash
# Install root helper so the guide can quiet console, set CPU governor, tune boot.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sudo install -m 755 "$ROOT/deploy/iptv-console" /usr/local/sbin/iptv-console
sudo tee /etc/sudoers.d/iptv-console >/dev/null <<'EOF'
# Guide service: console quiet/restore, CPU governor, one-shot OS tune, unit restart
pi ALL=(root) NOPASSWD: /usr/local/sbin/iptv-console
pi ALL=(root) NOPASSWD: /bin/systemctl restart iptv-guide.service, /bin/systemctl stop iptv-guide.service, /bin/systemctl start iptv-guide.service, /bin/systemctl is-active iptv-guide.service
EOF
sudo chmod 440 /etc/sudoers.d/iptv-console
sudo visudo -cf /etc/sudoers.d/iptv-console
echo "Installed. Test: sudo -n /usr/local/sbin/iptv-console cpu-performance"
echo "                 sudo -n /usr/local/sbin/iptv-console cpu-ondemand"
echo "                 sudo -n /usr/local/sbin/iptv-console quiet"
