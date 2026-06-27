#!/bin/bash
#
# Hephaestus Autopilot - Continuous Multi-Agent Workflow Engine
#
# A fully automated pipeline that watches a design queue directory and processes
# each design through the full pipeline:
#   1. Product Requirements Extraction (context-aware)
#   2. Architecture & Design
#   3. Development
#   4. Adversarial Code Review
#   5. Security Review
#   6. QA Testing & Validation
#   7. Product Validation (final spec check)
#
# Designed to run for days/weeks, processing designs as they arrive.
# Drop .md or .txt files into the design queue to add new designs.
#
# Usage:
#   ./autopilot.sh --design-queue ./designs --project-path ./project
#   ./autopilot.sh --design-queue ./designs --project-path ./project --max-iterations 5
#   ./autopilot.sh --stop
#   ./autopilot.sh --status
#
# Options:
#   --design-queue DIR          Directory to watch for design documents (required)
#   --project-path DIR          Root directory for builds and features (required)
#   --max-iterations N          Maximum review-fix-QA iterations per design (default: 3)
#   --drop-db                   Drop database before starting
#   --no-frontend               Skip starting the frontend dashboard
#   --stop                      Stop all services
#   --status                    Show service status
#

set -e
set -o pipefail

HEPHAESTUS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$HEPHAESTUS_DIR/.venv/bin/python"
LOG_DIR="$HOME/.hephaestus/logs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1"; }
header() { echo -e "\n${BOLD}${CYAN}=== $1 ===${NC}\n"; }

check_python() {
    if [ ! -f "$PYTHON" ]; then
        err "Python not found at $PYTHON"
        err "Run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
        exit 1
    fi
}

install_deps() {
    log "Installing Python dependencies..."
    cd "$HEPHAESTUS_DIR"
    # Source Rust if available (needed for turbovec source builds)
    [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
    "$PYTHON" -m pip install -r requirements.txt --quiet 2>&1 | tail -5
    ok "Dependencies installed"
}

check_docker() {
    if docker info >/dev/null 2>&1; then
        return 0
    fi

    log "Starting Rancher Desktop..."
    open -a "Rancher Desktop" 2>/dev/null || true

    for i in $(seq 1 30); do
        sleep 2
        if docker info >/dev/null 2>&1; then
            ok "Docker ready"
            return 0
        fi
        echo -n "."
    done

    err "Docker not available after 60s. Start Rancher Desktop manually."
    return 1
}

start_qdrant() {
    if curl -s http://localhost:6333/ >/dev/null 2>&1; then
        ok "Qdrant already running on port 6333"
        return 0
    fi

    log "Starting Qdrant container..."
    if docker ps -a --format '{{.Names}}' | grep -q '^qdrant$'; then
        docker start qdrant >/dev/null 2>&1
    else
        docker run -d -p 6333:6333 --name qdrant qdrant/qdrant >/dev/null 2>&1
    fi

    for i in $(seq 1 15); do
        sleep 1
        if curl -s http://localhost:6333/ >/dev/null 2>&1; then
            ok "Qdrant ready"
            return 0
        fi
    done

    err "Qdrant failed to start"
    return 1
}

start_backend() {
    if lsof -ti :8300 >/dev/null 2>&1; then
        warn "Port 8300 in use - killing existing process"
        lsof -ti :8300 | xargs kill -9 2>/dev/null
        sleep 1
    fi

    mkdir -p "$LOG_DIR"

    log "Starting Hephaestus backend..."
    cd "$HEPHAESTUS_DIR"

    LLM_PROVIDER=openrouter \
    LLM_MODEL=xiaomi/mimo-v2.5 \
    DATABASE_PATH="$HEPHAESTUS_DIR/hephaestus.db" \
    EMBEDDING_BACKEND=fastembed \
    VECTOR_STORE_BACKEND=turbovec \
    TURBOVEC_DATA_DIR="$HEPHAESTUS_DIR/data/turbovec" \
    DESIGN_QUEUE_DIR="$DESIGN_QUEUE" \
    FEATURES_DIR="$PROJECT_PATH/features" \
    MCP_PORT=8300 \
    DEFAULT_CLI_TOOL=pi \
    PROJECT_ROOT="$PROJECT_PATH" \
    MAIN_REPO_PATH="$PROJECT_PATH" \
    WORKING_DIRECTORY="$PROJECT_PATH" \
    "$PYTHON" run_server.py > "$LOG_DIR/backend.log" 2>&1 &

    BACKEND_PID=$!
    log "Backend PID: $BACKEND_PID"

    log "Waiting for backend to become healthy..."
    for i in $(seq 1 20); do
        sleep 2
        if curl -s http://localhost:8300/health 2>/dev/null | grep -q '"healthy"'; then
            ok "Backend healthy on port 8300"
            return 0
        fi
    done

    warn "Backend not yet healthy (may still be starting). Check: tail $LOG_DIR/backend.log"
    return 0
}

start_frontend() {
    log "Starting frontend..."
    cd "$HEPHAESTUS_DIR/frontend"
    npm run dev > "$LOG_DIR/frontend.log" 2>&1 &
    FRONTEND_PID=$!
    log "Frontend PID: $FRONTEND_PID"
    log "Frontend: http://localhost:3000"
}

start_monitor() {
    log "Starting monitor..."
    cd "$HEPHAESTUS_DIR"
    "$PYTHON" run_monitor.py > "$LOG_DIR/monitor.log" 2>&1 &
    MONITOR_PID=$!
    log "Monitor PID: $MONITOR_PID"
}

stop_services() {
    log "Stopping services..."
    lsof -ti :8300 | xargs kill -9 2>/dev/null && ok "Backend stopped" || warn "No backend to stop"
    pkill -f "npm run dev" 2>/dev/null && ok "Frontend stopped" || warn "No frontend to stop"
    pkill -f "run_monitor.py" 2>/dev/null && ok "Monitor stopped" || warn "No monitor to stop"
    pkill -f "orchestrator.py" 2>/dev/null && ok "Orchestrator stopped" || warn "No orchestrator to stop"
}

show_status() {
    header "Service Status"

    # Show vector store status based on backend
    if [ "${VECTOR_STORE_BACKEND:-turbovec}" = "qdrant" ]; then
        if curl -s http://localhost:6333/ >/dev/null 2>&1; then
            ok "Qdrant:      running (port 6333)"
        else
            err "Qdrant:      not running"
        fi
    else
        ok "TurboVec:    local (no container)"
    fi

    if curl -s http://localhost:8300/health 2>/dev/null | grep -q '"healthy"'; then
        ok "Backend:     healthy (port 8300)"
    else
        err "Backend:     not running"
    fi

    if curl -s http://localhost:5173 >/dev/null 2>&1 || curl -s http://localhost:3000 >/dev/null 2>&1; then
        ok "Frontend:    running"
    else
        warn "Frontend:    not running"
    fi

    if pgrep -f "run_monitor.py" >/dev/null 2>&1; then
        ok "Monitor:     running"
    else
        warn "Monitor:     not running"
    fi

    if pgrep -f "orchestrator.py" >/dev/null 2>&1; then
        ok "Orchestrator: running"
    else
        warn "Orchestrator: not running"
    fi

    echo ""
}

show_queue_status() {
    local queue_dir="$1"

    if [ ! -d "$queue_dir" ]; then
        warn "Design queue directory not found: $queue_dir"
        return
    fi

    header "Design Queue"

    local total=$(find "$queue_dir" -maxdepth 1 -name "*.md" -o -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')

    if [ "$total" -eq 0 ]; then
        echo -e "  ${YELLOW}Queue empty${NC} - drop .md or .txt files into:"
        echo -e "  ${BOLD}$queue_dir${NC}"
    else
        echo -e "  ${BOLD}$total design(s) pending:${NC}"
        echo ""
        for f in "$queue_dir"/*.md "$queue_dir"/*.txt; do
            [ -f "$f" ] || continue
            local name=$(basename "$f" .md)
            name=$(basename "$name" .txt)
            local size=$(wc -c < "$f" | tr -d ' ')
            echo -e "    ${CYAN}$name${NC} (${size} bytes)"
        done
    fi

    echo ""
}

show_usage() {
    cat << EOF

${BOLD}${CYAN}Hephaestus Autopilot - Continuous Multi-Agent Workflow Engine${NC}

${BOLD}USAGE:${NC}
    $0 --project-path ./project

${BOLD}OPTIONS:${NC}
    --project-path DIR          Project directory (required)
    --design-queue DIR          Override design queue location (default: <project-path>/docs/design)
    --max-iterations N          Maximum iterations per design (default: 3)
    --drop-db                   Drop database before starting
    --no-frontend               Skip frontend dashboard
    --stop                      Stop all services
    --status                    Show service status
    --help                      Show this help

${BOLD}DESIGN QUEUE:${NC}
    Drop .md or .txt files into <project-path>/docs/design/
    The pipeline watches this directory and processes designs in
    modification-time order (oldest first).

${BOLD}LITELLM PROXY (Optional - for cost tracking):${NC}
    Set environment variables to route through LiteLLM proxy:

    export LITELLM_PROXY_URL=http://deneb-server:4000
    export LITELLM_API_KEY=sk-your-virtual-key
    export LITELLM_MASTER_KEY=sk-your-master-key

    When enabled, all LLM calls are routed through the proxy with a
    custom "user" field set to the feature name, enabling per-feature
    cost tracking. Costs are displayed in the HTML feature report.

${BOLD}EXAMPLES:${NC}
    # Start continuous pipeline (design queue at ./project/docs/design/)
    $0 --project-path ./project

    # With more iterations per design
    $0 --project-path ./project --max-iterations 5

    # With custom design queue location
    $0 --project-path ./project --design-queue ./my-designs

    # Check what's in the queue
    ls ./project/docs/design/

    # Check status
    $0 --status

    # Stop everything
    $0 --stop

${BOLD}DESIGN QUEUE:${NC}
    Drop .md or .txt files into the design queue directory.
    The pipeline will automatically pick up the next design and process it.

    Example:
        cp my-feature-design.md ./designs/
        # Pipeline picks it up and starts processing

${BOLD}PIPELINE PHASES:${NC}
    1. Product Requirements  - Context-aware extraction from design docs
    2. Architecture & Design - Technical spec respecting existing system
    3. Development           - Implementation with tests
    4. Adversarial Review    - Critical code review and fixes
    5. Security Review       - Vulnerability assessment and fixes
    6. QA Validation         - Comprehensive testing
    7. Product Validation    - Final spec compliance check
    8. Git Commit & Push     - Branch, commit, merge to main
    9. Forensics Analysis    - Pipeline self-improvement review

${BOLD}OUTPUT:${NC}
    Each design produces:
    - features/<name>/              - Reports, docs, HTML report
    - features/<name>/docs/         - Requirements, architecture, review docs
    - features/<name>/feature_report.html - Human review report
    - <project-path>/               - Implementation code (src/, tests/, etc.)

${BOLD}STOP CONDITIONS:${NC}
    - Product validation passes (SUCCESS - moves to next design)
    - Hard error (crashed agents, critical failures)
    - Impasse (stuck agents, no progress)
    - Major architectural issue detected
    - Maximum iterations reached
    - API credits exhausted
    - Queue empty (pauses until new design arrives)

EOF
}

validate_inputs() {
    local valid=true

    if [ -z "$PROJECT_PATH" ]; then
        err "Missing required: --project-path"
        valid=false
    fi

    if [ "$valid" = false ]; then
        echo ""
        show_usage
        exit 1
    fi

    # Default design queue to <project-path>/docs/design
    if [ -z "$DESIGN_QUEUE" ]; then
        DESIGN_QUEUE="$PROJECT_PATH/docs/design"
    fi

    mkdir -p "$PROJECT_PATH"
    mkdir -p "$DESIGN_QUEUE"
}

run_autopilot() {
    header "AUTOPILOT CONTINUOUS PIPELINE"

    echo -e "${BOLD}Design Queue:${NC}  $DESIGN_QUEUE"
    echo -e "${BOLD}Project Root:${NC}  $PROJECT_PATH"
    echo -e "${BOLD}Max Iterations:${NC} $MAX_ITERATIONS"
    echo ""

    show_queue_status "$DESIGN_QUEUE"

    check_python
    install_deps

    # Only start Docker/Qdrant if using qdrant backend
    if [ "${VECTOR_STORE_BACKEND:-turbovec}" = "qdrant" ]; then
        check_docker || exit 1
        start_qdrant || exit 1
    fi

    start_backend || exit 1
    start_monitor
    start_frontend

    show_status

    header "LAUNCHING ORCHESTRATOR"

    ORCHESTRATOR_ARGS=(
        --design-queue "$DESIGN_QUEUE"
        --project-path "$PROJECT_PATH"
        --max-iterations "$MAX_ITERATIONS"
    )

    if [ "$DROP_DB" = true ]; then
        ORCHESTRATOR_ARGS+=(--drop-db)
    fi

    # Pass LiteLLM proxy config if set
    if [ -n "$LITELLM_PROXY_URL" ]; then
        export LITELLM_PROXY_URL
        export LITELLM_API_KEY="${LITELLM_API_KEY:-}"
        export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-}"
        export LITELLM_COST_TRACKING="${LITELLM_COST_TRACKING:-true}"
        log "LiteLLM proxy: $LITELLM_PROXY_URL (cost tracking: $LITELLM_COST_TRACKING)"
    fi

    log "Starting continuous pipeline..."
    log "The pipeline will watch for new designs in: $DESIGN_QUEUE"
    log "Drop .md or .txt files to add designs."
    log "Press Ctrl+C to stop."
    echo ""

    cd "$HEPHAESTUS_DIR"
    "$PYTHON" -m src.autopilot.orchestrator "${ORCHESTRATOR_ARGS[@]}"
    EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        ok "Pipeline stopped cleanly"
    else
        err "Pipeline exited with code $EXIT_CODE"
    fi

    echo ""
    header "FINAL STATUS"
    show_status
    show_queue_status "$DESIGN_QUEUE"

    log "Logs: $HOME/.hephaestus/autopilot/"
    log "Features: $PROJECT_PATH/features/"
}

ACTION="start"
DESIGN_QUEUE=""
PROJECT_PATH=""
MAX_ITERATIONS=3
DROP_DB=false
FRONTEND=true

while [[ $# -gt 0 ]]; do
    case $1 in
        --stop) ACTION="stop"; shift ;;
        --status) ACTION="status"; shift ;;
        --help) ACTION="help"; shift ;;
        --design-queue) DESIGN_QUEUE="$2"; shift 2 ;;
        --project-path) PROJECT_PATH="$2"; shift 2 ;;
        --max-iterations) MAX_ITERATIONS="$2"; shift 2 ;;
        --drop-db) DROP_DB=true; shift ;;
        --no-frontend) FRONTEND=false; shift ;;
        *) err "Unknown option: $1"; show_usage; exit 1 ;;
    esac
done

case $ACTION in
    stop)
        stop_services
        ;;
    status)
        show_status
        ;;
    help)
        show_usage
        ;;
    start)
        validate_inputs
        run_autopilot
        ;;
esac
