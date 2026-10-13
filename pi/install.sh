#!/bin/bash
# Set up a Raspberry Pi (Raspberry Pi OS desktop, auto-login on) to show the eye fullscreen.
# Usage, on the Pi, from this folder:   ./install.sh http://192.168.1.20:8085/
# (use the address of the computer running tracker.py). Then restart the Pi.
set -e
URL="$1"
if [[ "$URL" != http* ]]; then
  echo "Usage: ./install.sh http://<tracker-computer-ip>:8085/"
  exit 1
fi
cd "$(dirname "$0")"
sed "s|^URL=.*|URL=\"$URL\"|" eye-kiosk.sh > ~/eye-kiosk.sh
chmod +x ~/eye-kiosk.sh
mkdir -p ~/.config/labwc ~/.config/kanshi
cp labwc-autostart ~/.config/labwc/autostart      # replaces the normal desktop with the eye
cp kanshi-config ~/.config/kanshi/config           # screen at 720p (smoother on a Pi 4)
python3 invisible-cursor.py                        # hide the mouse pointer
echo "Done. Restart the Pi and it will boot straight into the eye at $URL"
