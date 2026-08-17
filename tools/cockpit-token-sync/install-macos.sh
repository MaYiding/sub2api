#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/mini-relay/launchd-scripts"
WRAPPER="$SUPPORT_DIR/sub2api-token-sync.sh"
PLIST="$HOME/Library/LaunchAgents/com.toooa.sub2api.cockpit-token-sync.plist"

mkdir -p "$SUPPORT_DIR" "$HOME/Library/LaunchAgents"

cat >"$WRAPPER" <<EOF
#!/bin/bash
set -u
while true; do
  /opt/homebrew/bin/node "$ROOT/tools/cockpit-token-sync/sync.js" || true
  sleep 30
done
EOF
chmod 700 "$WRAPPER"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.toooa.sub2api.cockpit-token-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$WRAPPER</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/sub2api-cockpit-token-sync.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/sub2api-cockpit-token-sync.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/com.toooa.sub2api.cockpit-token-sync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "loaded $PLIST"
