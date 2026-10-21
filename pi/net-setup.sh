#!/bin/bash
# Keeps the display Pi on Wi-Fi, and keeps its log across restarts. Run with sudo (install.sh does).
# Without this, a Wi-Fi drop can leave the Pi showing a "password required" box instead of the
# eye until someone power-cycles it (2026-10-20). Safe to run again.
set -e
cd "$(dirname "$0")"
# Wi-Fi power saving off: it causes the drops.
printf "[connection]\nwifi.powersave = 2\n" > /etc/NetworkManager/conf.d/wifi-powersave-off.conf
# Keep the log across restarts (Raspberry Pi OS forgets it at every restart), capped at 100 MB / 1 month.
mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nStorage=persistent\nSystemMaxUse=100M\nMaxRetentionSec=1month\n" \
  > /etc/systemd/journald.conf.d/50-persistent.conf
systemctl restart systemd-journald
# The watchdog reconnects Wi-Fi after 2 minutes offline, and restarts the Pi after 10.
install -m 755 net-watchdog.sh /usr/local/bin/eye-net-watchdog.sh
cp eye-net-watchdog.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable eye-net-watchdog
systemctl restart eye-net-watchdog
nmcli general reload conf
echo "Wi-Fi watchdog on (journalctl -u eye-net-watchdog), power saving off, log kept across restarts."
