#!/bin/bash
# Set up the Giant Eye yard speaker on a Raspberry Pi (Raspberry Pi OS Lite).
# Copy this folder to the Pi as ~/eye-speaker, pair the Bluetooth speaker (see README), then:
#   ./install.sh http://<tracker-computer>:8085 <speaker Bluetooth address>
# e.g. ./install.sh http://my-mac.local:8085 AA:BB:CC:11:22:33
set -e
SERVER=${1:?usage: ./install.sh http://<tracker-computer>:8085 <speaker address>}
MAC=${2:?usage: ./install.sh http://<tracker-computer>:8085 <speaker address>}
cd "$(dirname "$0")"

sudo rfkill unblock bluetooth || true
sudo apt-get install -y -qq bluez-alsa-utils mpg123 alsa-utils python3-aiohttp
bluetoothctl trust "$MAC" >/dev/null

# Medium Bluetooth audio quality: the Pi Zero's Wi-Fi and Bluetooth share one radio, and the
# default (high) crackles; medium sounded best in testing.
# hfp-ag + Bluetooth's (experimental) battery reporting: many speakers report their battery
# over the hands-free connection, and it's shown on the phone page. Music still uses A2DP.
sudo sed -i "s/^#Experimental = false/Experimental = true/" /etc/bluetooth/main.conf
sudo mkdir -p /etc/systemd/system/bluealsa.service.d
printf "[Service]\nExecStart=\nExecStart=/usr/bin/bluealsa -S -p a2dp-source -p a2dp-sink -p hfp-ag --sbc-quality=medium\n" \
  | sudo tee /etc/systemd/system/bluealsa.service.d/quality.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart bluetooth bluealsa

# Keep the log across restarts (Raspberry Pi OS keeps it in memory only), so if the speaker
# drops out overnight, power-cycling the Pi doesn't erase why. Capped to spare the SD card.
sudo mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nStorage=persistent\nSystemMaxUse=100M\nMaxRetentionSec=1month\n" \
  | sudo tee /etc/systemd/journald.conf.d/50-persistent.conf >/dev/null
sudo systemctl restart systemd-journald

# Stay on Wi-Fi: the Pi Zero's Wi-Fi chip drops off now and then with power saving on, and
# NetworkManager can give up rejoining for good; the watchdog reconnects (2 min) or restarts (10 min).
printf "[connection]\nwifi.powersave = 2\n" | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null
sudo cp eye-net-watchdog.service /etc/systemd/system/

sed -e "s|__EYE_SERVER__|$SERVER|" -e "s|__SPEAKER_MAC__|$MAC|" eye-speaker.service \
  | sudo tee /etc/systemd/system/eye-speaker.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now eye-speaker eye-net-watchdog
sudo systemctl restart eye-net-watchdog
echo "Yard speaker installed. Log: journalctl -u eye-speaker -f (Wi-Fi watchdog: -u eye-net-watchdog)"
echo "Wi-Fi power saving is off from the next restart (sudo reboot)."
