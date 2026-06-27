#!/usr/bin/env bash
# Smoke test B: run the full 10-phase autopilot pipeline end-to-end.
# Usage: ./scripts/smoke_run_b.sh [--keep]
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_PATH="/private/tmp/heph-smoke-test"
DB="hephaestus.db"
KEEP=false
[[ "${1:-}" == "--keep" ]] && KEEP=true

LOG_FILE="${SMOKE_LOG:-/tmp/smoke_run_b_$(date +%Y%m%d_%H%M%S).log}"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "Log: $LOG_FILE"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)]${NC} $*"; }
err()  { echo -e "${RED}[$(date +%H:%M:%S)]${NC} $*" >&2; }

# ─── Preflight ────────────────────────────────────────────────────────
[[ -d "$PROJECT_PATH/.git" ]] || { err "No repo at $PROJECT_PATH"; exit 1; }
[[ -d "$PROJECT_PATH/docs" ]] || { err "No docs/ dir at $PROJECT_PATH — check smoke-baseline"; exit 1; }

# ─── Stop services ────────────────────────────────────────────────────
log "Stopping services..."
pkill -9 -f run_server.py 2>/dev/null || true
pkill -9 -f "uvicorn src.mcp.server" 2>/dev/null || true
pkill -9 -f run_monitor.py 2>/dev/null || true
pkill -9 -f "heph autopilot" 2>/dev/null || true
sleep 2

# ─── Reset test repo ──────────────────────────────────────────────────
log "Resetting test repo..."
cd "$PROJECT_PATH"
for wt in $(git worktree list --porcelain 2>/dev/null | grep '^worktree ' | awk '{print $2}' | tail -n +2); do
    git worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
done
git worktree prune 2>/dev/null || true
rm -rf "$PROJECT_PATH/.worktrees/"*
git branch 2>/dev/null | sed 's/[*+]//' | tr -d ' ' | grep -E '^(agent-|feature/)' | xargs -I{} git branch -D {} 2>/dev/null || true
if git rev-parse smoke-baseline >/dev/null 2>&1; then
    git checkout main 2>/dev/null || true
    git reset --hard smoke-baseline
    git clean -fd 2>/dev/null || true
    log "Reset to smoke-baseline"
else
    warn "smoke-baseline tag not found — skipping repo reset"
fi
cd - >/dev/null

# Remove stale seeded test from baseline (test_compute.py is from a prior design)
rm -f "$PROJECT_PATH/tests/test_compute.py"

# Write canonical design doc and seeded failing test (overwrite each run)
mkdir -p "$PROJECT_PATH/docs/design-queue" "$PROJECT_PATH/tests"

cat > "$PROJECT_PATH/docs/design-queue/add_calculator.md" << 'DESIGN'
# Feature: add()

Add an `add(a, b)` function that returns the sum of two numbers, with
structured logging so every call is traceable.

## Requirements
- Module named `calculator` with a single function `add(a, b)`
- `add(a, b)` returns `a + b`
- Every call to `add()` must be logged using Python's `logging` module at
  INFO level: `"add called: a=<a>, b=<b>, result=<result>"`
- Log output goes to both stdout and a file `calculator.log` in the working
  directory using a FileHandler + StreamHandler on the `calculator` logger
- Logger name is `"calculator"` (not root logger)
- A runnable entry-point script `run_calculator.py` in the project root that:
  - Calls `add(2, 3)` and prints the result
  - Can be run with `python run_calculator.py`
  - Exits 0 on success

## File Layout
```
calculator.py        # the module
run_calculator.py    # entry-point script
tests/
  test_calculator.py # test suite (provided below — do not modify)
calculator.log       # written at runtime by the logger
```

## Test
```python
# tests/test_calculator.py — use exactly this file, do not add other test files
import logging
from calculator import add

def test_add():
    assert add(2, 3) == 5

def test_add_negative():
    assert add(-1, 1) == 0

def test_add_logging(caplog):
    with caplog.at_level(logging.INFO, logger="calculator"):
        result = add(10, 20)
    assert result == 30
    assert "add called" in caplog.text
```

## Acceptance Criteria
- `python run_calculator.py` exits 0, prints a log line to stdout, and writes
  to `calculator.log`
- `python -m pytest -p no:libtmux -v` runs and all 3 tests pass
- `calculator.log` contains at least one line matching
  `add called: a=<n>, b=<n>, result=<n>`
DESIGN

cat > "$PROJECT_PATH/tests/test_calculator.py" << 'PYTEST'
import logging
from calculator import add

def test_add():
    assert add(2, 3) == 5

def test_add_negative():
    assert add(-1, 1) == 0

def test_add_logging(caplog):
    with caplog.at_level(logging.INFO, logger="calculator"):
        result = add(10, 20)
    assert result == 30
    assert "add called" in caplog.text
PYTEST

cat > "$PROJECT_PATH/TESTING.md" << 'TESTING'
# Testing Guide: Calculator

## How to Run the Application

```bash
# Run the entry-point script (must exist at project root)
python run_calculator.py
# Expected: log line printed to stdout AND exit code 0

# View the log file written by the module
cat calculator.log
```

## How to Run Tests

```bash
# ALWAYS use -p no:libtmux (dev-environment plugin conflict — not a code bug)
python -m pytest -p no:libtmux -v 2>&1 | tee docs/test_results.txt

# With visible log output
python -m pytest -p no:libtmux -v --log-cli-level=INFO 2>&1 | tee docs/test_results_verbose.txt
```

## Log Locations

| Log | Path | Contents |
|-----|------|----------|
| Calculator runtime log | `./calculator.log` | INFO lines for every add() call |
| Test run output | `./docs/test_results.txt` | Full pytest output |

## Known Issues / Workarounds

- **libtmux pytest plugin**: always pass `-p no:libtmux` to pytest.
- **git index.lock**: run `rm -f .git/index.lock` then retry git commands.

## Smoke Test (Quick Validation)

```bash
# 1. Run the entry-point
python run_calculator.py
echo "Exit code: $?"

# 2. Verify calculator.log was written
python -c "
import os, sys
assert os.path.exists('calculator.log'), 'FAIL: calculator.log not written'
lines = open('calculator.log').readlines()
assert any('add called' in l for l in lines), 'FAIL: no log line found'
print('calculator.log OK')
"

# 3. Run the tests
python -m pytest -p no:libtmux -v
```

## Test Coverage Requirements

- `test_add`: basic sum — must pass
- `test_add_negative`: negative numbers — must pass
- `test_add_logging`: verifies structured log output — must pass

## Acceptance Criteria

All of the following must be true before QA can pass:
1. `python run_calculator.py` exits 0 and prints a log line to stdout
2. `calculator.log` exists and contains at least one line matching
   `add called: a=<n>, b=<n>, result=<n>`
3. `python -m pytest -p no:libtmux -v` exits 0 (all 3 tests green)
TESTING

# ─── Reset DB + state ─────────────────────────────────────────────────
log "Clearing state..."
rm -rf ~/.hephaestus/autopilot/run-* ~/.hephaestus/autopilot/pipeline_state.json \
       ~/.hephaestus/autopilot/processed_designs.json \
       ~/.hephaestus/autopilot/state.json ~/.hephaestus/autopilot/input_*.json \
       /tmp/hephaestus_worktrees/*
> hephaestus_server.log 2>/dev/null || true
> ~/.hephaestus/logs/monitor.log 2>/dev/null || true
> logs/monitor.log 2>/dev/null || true

sqlite3 "$DB" "
    PRAGMA trusted_schema=ON;
    DELETE FROM tasks;
    DELETE FROM phase_executions;
    DELETE FROM phases;
    DELETE FROM workflows;
    DELETE FROM agents WHERE agent_type IN ('phase', 'orchestrator');
    DELETE FROM ticket_comments;
    DELETE FROM tickets;
    UPDATE autopilot_designs SET status='pending', completed_at=NULL
    WHERE project_id='proj-3c562e7de665';
" 2>/dev/null

# ─── Start services ───────────────────────────────────────────────────
log "Starting services..."
.venv/bin/heph start 2>&1

log "Waiting for backend..."
for i in $(seq 1 24); do
    sleep 5
    H=$(curl -s http://127.0.0.1:8300/health 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    [[ "$H" == "healthy" ]] && log "Backend healthy" && break
    [[ $i -eq 24 ]] && err "Backend never became healthy" && exit 1
done

# ─── Start pipeline ───────────────────────────────────────────────────
log "Starting pipeline..."
curl -s -X POST "http://127.0.0.1:8300/api/autopilot/start?project_path=$PROJECT_PATH&max_iterations=3" \
    | python3 -m json.tool 2>/dev/null || { err "Failed to start pipeline"; exit 1; }

# ─── Poll loop ────────────────────────────────────────────────────────
POLL=30; MAX=$((60 * 120 / POLL))  # 2-hour cap

q() { sqlite3 "$DB" "$1" 2>/dev/null; }

phases() {
    q "SELECT p.name || ':' || pe.status
       FROM phase_executions pe JOIN phases p ON pe.phase_id = p.id
       WHERE pe.workflow_execution_id = (
           SELECT id FROM workflows WHERE status IN ('active','paused')
           ORDER BY rowid DESC LIMIT 1
       ) ORDER BY p.\"order\"" | tr '\n' ' '
}

for i in $(seq 1 $MAX); do
    sleep $POLL
    E=$((i * POLL))

    R=$(curl -s http://127.0.0.1:8300/api/autopilot/status 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('running','?'))" 2>/dev/null)
    P=$(phases)
    DONE=$(q "SELECT count(*) FROM phase_executions WHERE status='completed'")
    FAIL=$(q "SELECT count(*) FROM tasks WHERE status='failed'")
    AGENTS=$(q "SELECT count(*) FROM agents WHERE agent_type='phase' AND status IN ('working','idle','starting')")

    echo "[${E}s] run=$R agents=$AGENTS done=$DONE failed=$FAIL | $P"

    # Diagnostics every 2 min
    if (( E % 120 == 0 )); then
        TASK_FAIL=$(q "SELECT count(*) FROM tasks WHERE status='failed'")
        echo "  [diag] failed_tasks=$TASK_FAIL"
        NOW=$(date +%Y-%m-%d)
        LAST_ERR=$(grep "$NOW" hephaestus_server.log 2>/dev/null | grep -h "Failed to create agent\|Marked task.*failed" | tail -1 || true)
        [[ -n "$LAST_ERR" ]] && echo "  [diag] $LAST_ERR"
        AGENT_ID=$(q "SELECT substr(id,1,8) FROM agents WHERE agent_type='phase' AND status='working' ORDER BY rowid DESC LIMIT 1")
        if [[ -n "$AGENT_ID" ]]; then
            AGENT_OUT=$(tmux capture-pane -t "agent_${AGENT_ID}" -p -S -5 2>/dev/null | tail -3 | tr '\n' ' ' | head -c 200 || true)
            [[ -z "$AGENT_OUT" ]] && AGENT_OUT=$(tmux capture-pane -t "agent_${AGENT_ID}_r" -p -S -5 2>/dev/null | tail -3 | tr '\n' ' ' | head -c 200 || true)
            [[ -n "$AGENT_OUT" ]] && echo "  [agent] $AGENT_OUT"
        fi
    fi

    # Log analysis every 5 min
    if (( E % 300 == 0 )) && [[ $i -gt 1 ]]; then
        echo ""
        echo "  === LOG ANALYSIS (t=${E}s) ==="
        NOW=$(date +%Y-%m-%d)
        ERR_COUNT=$(grep "$NOW" hephaestus_server.log 2>/dev/null | grep -c "ERROR" || true)
        echo "  [logs] server_errors=$ERR_COUNT"
        RESTARTS=$(grep "$NOW" ~/.hephaestus/logs/monitor.log 2>/dev/null | grep -c "restarted successfully" || true)
        echo "  [logs] agent_restarts=$RESTARTS"
        GARBLED=$(grep "$NOW" ~/.hephaestus/logs/monitor.log 2>/dev/null | grep -c "garbled\|exited to command" || true)
        echo "  [logs] garbled_exited=$GARBLED"
        MCP_FAIL=$(grep "$NOW" hephaestus_server.log 2>/dev/null | grep -c "Failed to process task" || true)
        echo "  [logs] mcp_task_failures=$MCP_FAIL"
        TASK_DONE=$(q "SELECT count(*) FROM tasks WHERE status='done'")
        TASK_FAIL=$(q "SELECT count(*) FROM tasks WHERE status='failed'")
        TASK_STUCK=$(q "SELECT count(*) FROM tasks WHERE status='in_progress' AND started_at < datetime('now', '-10 minutes')")
        echo "  [tasks] done=$TASK_DONE failed=$TASK_FAIL stuck=$TASK_STUCK"
        PHASE_DONE=$(q "SELECT count(*) FROM phase_executions WHERE status='completed'")
        PHASE_FAIL=$(q "SELECT count(*) FROM phase_executions WHERE status='failed'")
        echo "  [phases] done=$PHASE_DONE failed=$PHASE_FAIL"
        set +o pipefail
        MON_ALIVE=$(ps aux | grep run_monitor | grep -v grep | wc -l | tr -d ' ')
        set -o pipefail
        echo "  [monitor] alive=${MON_ALIVE:-0}"
        if [[ -f ~/.hephaestus/logs/monitor_heartbeat ]]; then
            HB=$(cat ~/.hephaestus/logs/monitor_heartbeat 2>/dev/null)
            AGE=$(( $(date +%s) - ${HB%.*} ))
            echo "  [monitor] heartbeat ${AGE}s ago"
            (( AGE > 120 )) && echo "  [monitor] WARNING: heartbeat stale — monitor may be dead!"
        else
            echo "  [monitor] no heartbeat file"
        fi
        SRV=$(curl -s http://127.0.0.1:8300/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "dead")
        echo "  [server] status=$SRV"
        echo "  === END LOG ANALYSIS ==="
        echo ""
    fi

    [[ "$R" == "False" ]] && [[ $i -gt 4 ]] && log "Pipeline stopped" && break
    [[ "$DONE" == "10" ]] && log "All 10 phases complete!" && break
done

# ─── Final report ─────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo "                  RUN B RESULTS"
echo "═══════════════════════════════════════════════════"
q "SELECT '  ' || p.name || ': ' || pe.status
   FROM phase_executions pe JOIN phases p ON pe.phase_id = p.id
   WHERE pe.workflow_execution_id = (SELECT id FROM workflows ORDER BY rowid DESC LIMIT 1)
   ORDER BY p.\"order\""

echo ""
echo "GOTO / SPEC-GATE events:"
grep -h "SPEC-GATE\|GOTO\|mark_phase_complete" hephaestus_server.log 2>/dev/null \
    | tail -10 | sed 's/^/  /' || echo "  (none)"

echo ""
RUN_DIR=$(ls -dt ~/.hephaestus/autopilot/run-* 2>/dev/null | head -1)
[[ -n "$RUN_DIR" ]] && echo "Orchestrator (last 10):" && tail -10 "$RUN_DIR/orchestrator.log" 2>/dev/null | sed 's/^/  /'

echo "═══════════════════════════════════════════════════"

$KEEP || pkill -9 -f "pi.*approve" 2>/dev/null || true
log "Done."
