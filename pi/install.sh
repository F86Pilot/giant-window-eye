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
mkdir -p ~/.config/wf-panel-pi
cp wf-panel-pi.ini ~/.config/wf-panel-pi/        # empty taskbar: no pop-ups over the eye
python3 invisible-cursor.py                        # hide the mouse pointer
sudo ./net-setup.sh                                # stay on Wi-Fi (watchdog), keep the log
echo "Done. Restart the Pi and it will boot straight into the eye at $URL"
