#!/bin/bash
# Keeps the yard speaker Pi on Wi-Fi. After a few failed tries to rejoin, NetworkManager can
# decide the Wi-Fi password is wrong and stop trying for good, leaving the Pi offline until
# someone power-cycles it. This checks every 30 s that the router answers; after 2 minutes
# without it, it reconnects Wi-Fi (every 2 minutes), and after 10 minutes it restarts the Pi
# (at most once an hour, so a router that's off for the night doesn't cause a restart loop).
# Installed by install.sh as the eye-net-watchdog service. Log: journalctl -u eye-net-watchdog
CHECK=30
RECONNECT_AFTER=120
REBOOT_AFTER=600
REBOOT_GAP=3600
STAMP=/var/lib/eye-net-watchdog/last-reboot

down_since=0
tried=0
while sleep "$CHECK"; do
  gw=$(ip route show default dev wlan0 2>/dev/null | awk '{print $3; exit}')
  if [ -n "$gw" ] && ping -c 1 -W 3 "$gw" >/dev/null 2>&1; then
    if [ "$down_since" -ne 0 ]; then
      echo "network back after $(( $(date +%s) - down_since )) s"
    fi
    down_since=0
    tried=0
    continue
  fi
  now=$(date +%s)
  if [ "$down_since" -eq 0 ]; then
    down_since=$now
    echo "can't reach the router${gw:+ ($gw)}"
  fi
  out=$(( now - down_since ))
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  if [ "$out" -ge "$REBOOT_AFTER" ] && [ $(( now - last )) -ge "$REBOOT_GAP" ]; then
    echo "offline for $out s, restarting the Pi"
    mkdir -p "$(dirname "$STAMP")"
    echo "$now" > "$STAMP"
    sync
    systemctl reboot
    exit 0
  fi
  if [ "$out" -ge "$RECONNECT_AFTER" ] && [ $(( now - tried )) -ge "$RECONNECT_AFTER" ]; then
    tried=$now
    echo "offline for $out s, reconnecting Wi-Fi"
    nmcli radio wifi on
    nmcli --colors no device connect wlan0 2>&1 | tr -d "\r" | tail -1
  fi
done
