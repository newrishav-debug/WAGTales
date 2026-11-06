#!/bin/bash
# WAGTales Tracker: installs or updates the app on this Mac and keeps it running at
# http://127.0.0.1:8765 every time you log in.
#   Install or update:  bash install.sh
#   Stop and remove:    bash install.sh uninstall   (your data in ~/WAGTales/data is kept)
#
# The app lives in ~/WAGTales/app and the data in ~/WAGTales/data, outside this folder,
# because macOS does not let login items read the Desktop.
set -e

PORT=8765
HOME_DIR="$HOME/WAGTales"
APP="$HOME_DIR/app"
DATA="$HOME_DIR/data"
LABEL="com.wagtales.local"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/wagtales-local.log"
UIDN="$(id -u)"
SRC="$(cd "$(dirname "$0")" && pwd)/app"

if [ "$1" = "uninstall" ]; then
  launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Stopped the app and removed the login item. Your data is still in $DATA."
  exit 0
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import http.server' >/dev/null 2>&1; then
  echo "Python 3 is needed and was not found. If a window asks to install the Command Line Tools, accept it,"
  echo "wait for it to finish, then run this script again."
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "gui/$UIDN/$LABEL" >/dev/null 2>&1; then
  echo "Port $PORT is already used by another program. Edit PORT at the top of this script and run it again."
  exit 1
fi

mkdir -p "$APP" "$DATA" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/server.py" "$SRC/index.html" "$SRC/apps-script.gs" "$APP/"
# files from the earlier version, which served a page that talked to the sheet directly
rm -f "$HOME_DIR/index.html" "$HOME_DIR/apps-script.txt"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP/server.py</string>
    <string>--data</string>
    <string>$DATA</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF

if [ -n "$WAGTALES_NO_LAUNCH" ]; then
  echo "Files written (launch skipped)."
  exit 0
fi

launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
# the old copy takes a moment to stop; starting before it has gone fails with "Input/output error"
for i in 1 2 3 4 5 6 7 8 9 10; do
  launchctl print "gui/$UIDN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
for i in 1 2 3 4 5; do
  launchctl bootstrap "gui/$UIDN" "$PLIST" 2>/dev/null && break
  [ "$i" = 5 ] && { echo "Could not start the app with launchd. Try: bash install.sh again."; exit 1; }
  sleep 1
done
launchctl kickstart -k "gui/$UIDN/$LABEL" 2>/dev/null || true

for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "WAGTales is running at http://127.0.0.1:$PORT and starts again each time you log in."
    echo "Data: $DATA"
    [ -z "$WAGTALES_NO_OPEN" ] && open "http://127.0.0.1:$PORT/" 2>/dev/null || true
    exit 0
  fi
  sleep 1
done
echo "The app did not answer. Look at $LOG for the reason."
exit 1
