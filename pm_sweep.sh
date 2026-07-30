#!/bin/bash
# The PM board's scheduled sweep (2026-07-30).
#
# ONE LaunchAgent, one schedule, and the MODE is decided here from the clock —
# rather than three competing agents that can fire in the wrong order or drift
# apart when the schedule changes. Her hours are 8–4:
#
#   07:45  pre-arrival  → sweep, ingest, and OPEN the board (her standing ask:
#                         the briefing ready at 8am, already open, refreshed)
#   08:00–16:00 :00/:30 → poll; model work happens only on a non-empty poll
#   15:45  wrap         → the last read of the day; the daily report's input
#
# launchd's StartCalendarInterval fires on LOCAL wall-clock time, so 07:45 stays
# 07:45 through the November DST change. (The "cron is UTC and drifts an hour"
# note in the 2026-07-30 session log applies to cron, not to this.)
#
# A LaunchAgent does NOT inherit her shell profile, so nothing here may assume
# PATH. USER/LOGNAME are set in the plist because the connectors fail to
# authenticate without them.
set -uo pipefail

ROOT="/Users/Hadassa/rfs_pm"
LOG="$ROOT/logs/sweep.log"
mkdir -p "$ROOT/logs"

HHMM=$(date +%H%M)
MODE=""
case "$HHMM" in
  074*|075*) MODE="--open-browser" ;;   # the pre-arrival run
  154*|155*) MODE="--wrap" ;;           # the end-of-day wrap
esac

echo "── $(date '+%Y-%m-%d %H:%M:%S') sweep ${MODE:-poll} ──" >> "$LOG"
# Never `set -e` around this: a failing sweep must still reach the reporting
# below, because run_sweep() records the failure into state for the on-page
# banner. Exiting early would leave the board quietly stale instead of loudly
# wrong, which is the whole thing this is built to prevent.
/usr/bin/python3 "$ROOT/pm_sweep_run.py" $MODE >> "$LOG" 2>&1
RC=$?
echo "   exit=$RC" >> "$LOG"

# Keep the log from growing without bound; a scheduled job that fills a disk is
# its own outage.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2000000 ]; then
  tail -c 500000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit $RC
