#!/bin/bash
# Install (or reinstall) the Giant Eye tracker as a background service on this Mac.
# Run after setting up ~/giant-eye (see README). Asks for your password once.
set -e
PLIST=/Library/LaunchDaemons/com.giant-eye.tracker.plist
TMP=$(mktemp)
sed "s|__HOME__|$HOME|g" "$(dirname "$0")/com.giant-eye.tracker.plist" > "$TMP"
plutil -lint "$TMP" >/dev/null

sudo launchctl bootout system/com.giant-eye.tracker 2>/dev/null || true
sudo install -o root -g wheel -m 644 "$TMP" "$PLIST"
rm "$TMP"
# The old copy can take a moment to stop; retry until it starts.
for i in 1 2 3 4 5; do
  sudo launchctl bootstrap system "$PLIST" 2>/dev/null && break
  sleep 2
done
echo "Giant Eye service installed. Log: $HOME/Library/Logs/giant-eye.log"
