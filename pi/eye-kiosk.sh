#!/bin/bash
# Giant Eye kiosk: shows the eye fullscreen and keeps it showing.
# Started automatically at login by ~/.config/labwc/autostart.
URL="http://YOUR-TRACKER-IP:8085/"   # the computer running tracker.py (pi/install.sh fills this in)
PROFILE="$HOME/.cache/eye-kiosk"   # the kiosk's own browser profile
exec >> "$HOME/eye-kiosk.log" 2>&1   # keep a log for troubleshooting

wait_for_server() {
  # After a power cut the Pi may boot before the server, so wait for it.
  until curl -s -o /dev/null --max-time 3 "$URL"; do sleep 3; done
}

while true; do
  wait_for_server
  # A power cut can leave a stale "browser already running" lock behind,
  # which makes Chromium refuse to start. Nothing else uses this profile,
  # so it's always safe to clear.
  rm -f "$PROFILE"/Singleton*
  echo "$(date): starting Chromium"
  chromium "$URL" \
    --user-data-dir="$PROFILE" \
    --kiosk --incognito --ozone-platform=wayland \
    --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --disable-features=Translate \
    --password-store=basic --check-for-update-interval=31536000 \
    $EYE_EXTRA_FLAGS
  echo "$(date): Chromium exited ($?), restarting"
  # If Chromium ever quits or crashes, start it again.
  sleep 3
done
