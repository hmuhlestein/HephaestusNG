#!/usr/bin/env bash
# Monitor supervisor + crash diagnostic.
# Runs run_monitor.py, logs the EXACT death reason (signal vs exit code) so we can
# tell SIGKILL(137)/SIGTERM(143)/segfault/clean-exit apart, snapshots memory at
# death, then restarts it. Run fully detached:  setsid ./scripts/monitor_supervisor.sh &
set -u
cd "$(dirname "$0")/.."
DEATHLOG="/tmp/monitor_death.log"
OUT="/tmp/monitor_wrapped.out"
HB="$HOME/.hephaestus/logs/monitor_heartbeat"

log() { echo "$(date '+%F %T') $*" >> "$DEATHLOG"; }

log "=== supervisor started (supervisor pid=$$) ==="
while true; do
    log "START run_monitor.py"
    .venv/bin/python run_monitor.py >> "$OUT" 2>&1 &
    MPID=$!
    log "  monitor pid=$MPID"
    wait "$MPID"
    code=$?
    if [ "$code" -gt 128 ]; then
        sig=$((code - 128))
        reason="KILLED BY SIGNAL $sig (exit $code)"
    else
        reason="exited code=$code"
    fi
    hb_age="n/a"; [ -f "$HB" ] && hb_age="$(( $(date +%s) - $(cut -d. -f1 < "$HB") ))s"
    log "DIED pid=$MPID $reason | last_heartbeat=${hb_age} ago"
    log "  vm_stat: $(vm_stat 2>/dev/null | grep -iE 'free|inactive|wired|compressed' | tr '\n' ' ')"
    log "  who-killed (recent kill audit, best-effort): $(log show --last 30s --predicate 'eventMessage CONTAINS \"'"$MPID"'\"' 2>/dev/null | grep -iE 'kill|term' | head -1 | cut -c1-100)"
    log "RESTARTING in 5s"
    sleep 5
done
