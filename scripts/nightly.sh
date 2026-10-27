#!/bin/zsh
# Nightly pipeline run, for launchd (installed by scripts/install_nightly.sh).
#
# Rescans data/notices/ for new or changed notices, fetches Companies House answers older
# than 23:59 again (an unchanged answer only moves its last-fetched date), reuses every other
# cached answer, and publishes only if every gate passes. A held or failed run keeps the last
# published votes, logs why, and raises a macOS notification.
set -u
ROOT="${0:A:h:h}"
LOGS="$HOME/Library/Logs/pennant"
LOG="$LOGS/nightly-$(date +%F).log"
mkdir -p "$LOGS"
cd "$ROOT" || exit 1
{
  print "== $(date '+%F %T %Z') pennant run --publish =="
  PENNANT_OFFLINE=0 .venv/bin/pennant run --publish
  code=$?
  print "== exit $code =="
} >>"$LOG" 2>&1
if (( code != 0 )); then
  osascript -e "display notification \"Nightly run held or failed: $LOG\" with title \"pennant\"" \
    >/dev/null 2>&1
fi
exit $code
