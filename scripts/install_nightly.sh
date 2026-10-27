#!/bin/zsh
# Install (or remove) the nightly pennant run as a launchd job for this user.
#
#   scripts/install_nightly.sh            install, running every night at 02:00
#   scripts/install_nightly.sh 5 30       install, running every night at 05:30
#   scripts/install_nightly.sh --remove   uninstall
#
# launchd rather than cron: a job missed while the Mac sleeps runs when it wakes.
set -eu
LABEL="com.pennant.nightly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="${0:A:h:h}"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$PLIST"
  print "Removed $LABEL."
  exit 0
fi
HOUR="${1:-2}"
MINUTE="${2:-0}"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/pennant"
cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>$ROOT/scripts/nightly.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/pennant/launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/pennant/launchd.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap "gui/$UID" "$PLIST"
printf "Installed %s: every night at %02d:%02d, logs in ~/Library/Logs/pennant/\n" "$LABEL" "$HOUR" "$MINUTE"
