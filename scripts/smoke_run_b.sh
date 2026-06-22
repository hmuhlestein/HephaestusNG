#!/usr/bin/env bash
# Run B smoke test: proves spec-gate-driven goto with real agents.
# Usage: ./scripts/smoke_run_b.sh [--keep]
#   --keep  Don't clean up after run (for debugging)

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_PATH="/tmp/heph-smoke-test"
DB="hephaestus.db"
KEEP=false
[[ "${1:-}" == "--keep" ]] && KEEP=true

# ─── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)]${NC} $*"; }
err()  { echo -e "${RED}[$(date +%H:%M:%S)]${NC} $*" >&2; }

# ─── Preflight ────────────────────────────────────────────────────────
if [[ ! -d "$PROJECT_PATH/.git" ]]; then
    err "Smoke test repo not found at $PROJECT_PATH"
    err "Run: mkdir -p $PROJECT_PATH && cd $PROJECT_PATH && git init"
    exit 1
fi

if [[ ! -f "$PROJECT_PATH/docs/design-queue/add_calculator.md" ]]; then
    err "Design doc missing: $PROJECT_PATH/docs/design-queue/add_calculator.md"
    exit 1
fi

# ─── Stop everything ──────────────────────────────────────────────────
log "Stopping all services..."
pkill -9 -f run_server.py 2>/dev/null || true
pkill -9 -f run_monitor.py 2>/dev/null || true
pkill -9 -f "heph autopilot" 2>/dev/null || true
pkill -9 -f "pi.*approve" 2>/dev/null || true
sleep 3

# ─── Clean state ──────────────────────────────────────────────────────
log "Cleaning state..."
rm -rf ~/.hephaestus/autopilot/run-*
rm -rf ~/.hephaestus/autopilot/pipeline_state.json
rm -rf ~/.hephaestus/autopilot/processed_designs.json
rm -rf ~/.hephaestus/autopilot/state.json
rm -rf ~/.hephaestus/autopilot/input_*.json
rm -rf /tmp/hephaestus_worktrees/*

# Clean project worktrees and agent branches
for wt in $(git -C "$PROJECT_PATH" worktree list --porcelain 2>/dev/null | grep '^worktree ' | awk '{print $2}' | tail -n +2); do
    git -C "$PROJECT_PATH" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
done
git -C "$PROJECT_PATH" worktree prune 2>/dev/null || true
git -C "$PROJECT_PATH" branch 2>/dev/null | grep 'agent-' | xargs -I{} git -C "$PROJECT_PATH" branch -D {} 2>/dev/null || true

sqlite3 "$DB" "
    DELETE FROM tasks;
    DELETE FROM phase_executions;
    DELETE FROM phases;
    DELETE FROM workflows;
    DELETE FROM agents WHERE agent_type IN ('phase', 'orchestrator');
    UPDATE autopilot_designs SET status='pending', completed_at=NULL
    WHERE project_id='proj-06a3e0670328';
" 2>/dev/null

# ─── Verify seeded test ──────────────────────────────────────────────
if [[ ! -f "$PROJECT_PATH/tests/test_compute.py" ]]; then
    warn "Seeded failing test missing, creating..."
    mkdir -p "$PROJECT_PATH/tests"
    cat > "$PROJECT_PATH/tests/test_compute.py" << 'PYTEST'
"""Seeded failing test — asserts a function that doesn't exist yet.
QA should report failed_tests >= 1, gate should send work back to development."""
from calculator import compute

def test_compute_returns_42():
    assert compute() == 42
PYTEST
fi

# ─── Start services ──────────────────────────────────────────────────
log "Starting services with heph..."
.venv/bin/heph start 2>&1

log "Waiting for backend health..."
for i in $(seq 1 24); do
    sleep 5
    H=$(curl -s http://127.0.0.1:8300/health 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    [[ "$H" == "healthy" ]] && log "Backend healthy" && break
    [[ $i -eq 24 ]] && err "Backend never became healthy" && exit 1
done

# ─── Verify single server ────────────────────────────────────────────
SERVER_COUNT=$(ps aux | grep run_server | grep -v grep | wc -l | tr -d ' ')
if [[ "$SERVER_COUNT" -gt 1 ]]; then
    warn "Multiple server processes detected ($SERVER_COUNT) — DB fallback will handle it"
fi

# ─── Start pipeline ──────────────────────────────────────────────────
log "Starting autopilot pipeline..."
curl -s -X POST "http://127.0.0.1:8300/api/autopilot/start?project_path=$PROJECT_PATH&max_iterations=3" \
    | python3 -m json.tool 2>/dev/null || { err "Failed to start pipeline"; exit 1; }

# ─── Monitor ─────────────────────────────────────────────────────────
POLL_INTERVAL=30
MAX_POLLS=60  # 30 minutes
STALE_COUNT=0
MAX_STALE=6  # 3 minutes of no progress → check deeper

get_phases() {
    sqlite3 "$DB" "
        SELECT p.name || ':' || pe.status
        FROM phase_executions pe
        JOIN phases p ON pe.phase_id = p.id
        WHERE pe.workflow_execution_id = (
            SELECT id FROM workflows
            WHERE status IN ('active','paused')
            ORDER BY rowid DESC LIMIT 1
        )
        ORDER BY p.\"order\"
    " 2>/dev/null | tr '\n' ' '
}

get_agent_count() {
    sqlite3 "$DB" "
        SELECT count(*) FROM agents
        WHERE agent_type='phase'
        AND status IN ('working','idle','starting')
    " 2>/dev/null
}

get_completed_count() {
    sqlite3 "$DB" "
        SELECT count(*) FROM phase_executions pe
        WHERE pe.workflow_execution_id = (
            SELECT id FROM workflows
            WHERE status IN ('active','paused','completed')
            ORDER BY rowid DESC LIMIT 1
        )
        AND pe.status = 'completed'
    " 2>/dev/null
}

prev_phases=""
for i in $(seq 1 $MAX_POLLS); do
    sleep $POLL_INTERVAL
    E=$((i * POLL_INTERVAL))

    R=$(curl -s http://127.0.0.1:8300/api/autopilot/status 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('running','?'))" 2>/dev/null)
    P=$(get_phases)
    A=$(get_agent_count)
    DONE=$(get_completed_count)
    S=$(grep -h "SPEC-GATE\|GOTO\|PHASE-PROGRESSION\|Created agent for phase\|hephaestus_update_task_status" \
        hephaestus_server.log 2>/dev/null | tail -1 || true)

    # Detect progress
    if [[ "$P" == "$prev_phases" ]]; then
        STALE_COUNT=$((STALE_COUNT + 1))
    else
        STALE_COUNT=0
        prev_phases="$P"
    fi

    echo "[$E s] run=$R agents=$A done=$DONE | $P | $S"

    # Diagnostics every 2 min
    if (( E % 120 == 0 )); then
        FAILED=$(sqlite3 "$DB" "SELECT count(*) FROM tasks WHERE status='failed'" 2>/dev/null)
        echo "  [diag] failed_tasks=$FAILED"
        # Show last error from server
        LAST_ERR=$(grep -h "Failed to create agent\|Marked task.*failed" hephaestus_server.log 2>/dev/null | tail -1 || true)
        [[ -n "$LAST_ERR" ]] && echo "  [diag] $LAST_ERR"
        # Show what agent is doing
        AGENT_ID=$(sqlite3 "$DB" "SELECT substr(id,1,8) FROM agents WHERE agent_type='phase' AND status='working' ORDER BY rowid DESC LIMIT 1" 2>/dev/null)
        if [[ -n "$AGENT_ID" ]]; then
            AGENT_OUT=$(tmux capture-pane -t "agent_${AGENT_ID}_r" -p -S -5 2>/dev/null | tail -3 | tr '\n' ' ' | head -c 200 || true)
            [[ -n "$AGENT_OUT" ]] && echo "  [agent] $AGENT_OUT"
        fi
    fi

    # Pipeline finished
    if [[ "$R" == "False" ]] && [[ $i -gt 4 ]]; then
        log "Pipeline stopped"
        break
    fi

    # All phases done
    if [[ "$DONE" == "10" ]]; then
        log "All 10 phases completed!"
        break
    fi
done

# ─── Final report ────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "                    RUN B RESULTS"
echo "═══════════════════════════════════════════════════════"
echo ""

echo "Phase Status:"
sqlite3 "$DB" "
    SELECT '  ' || p.name || ': ' || pe.status
    FROM phase_executions pe
    JOIN phases p ON pe.phase_id = p.id
    WHERE pe.workflow_execution_id = (
        SELECT id FROM workflows ORDER BY rowid DESC LIMIT 1
    )
    ORDER BY p.\"order\"
" 2>/dev/null

echo ""
echo "SPEC-GATE / GOTO Events:"
grep -h "SPEC-GATE\|GOTO\|score\|failed_tests\|mark_phase_complete" \
    hephaestus_server.log 2>/dev/null | tail -10 | sed 's/^/  /' || echo "  (none)"

echo ""
echo "Orchestrator Log (last 10 lines):"
RUN_DIR=$(ls -dt ~/.hephaestus/autopilot/run-* 2>/dev/null | head -1)
if [[ -n "$RUN_DIR" ]]; then
    tail -10 "$RUN_DIR/orchestrator.log" 2>/dev/null | sed 's/^/  /'
fi

echo ""
echo "═══════════════════════════════════════════════════════"

# ─── Cleanup ─────────────────────────────────────────────────────────
if [[ "$KEEP" == false ]]; then
    log "Cleaning up..."
    pkill -9 -f "pi.*approve" 2>/dev/null || true
fi

log "Done."
