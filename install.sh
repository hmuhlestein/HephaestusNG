#!/bin/bash
#
# Hephaestus Install Script
#
# Sets up the full Hephaestus environment:
#   - Python virtual environment
#   - Backend dependencies
#   - Qdrant vector store (Docker)
#   - Database initialization
#   - Frontend dashboard (Node.js)
#   - heph CLI command
#
# Usage:
#   ./install.sh              # Full install
#   ./install.sh --skip-docker  # Skip Docker/Qdrant setup
#   ./install.sh --skip-frontend  # Skip frontend setup
#   ./install.sh --dev        # Install with dev dependencies
#

set -e

HEPHAESTUS_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_MIN_VERSION="3.11"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${BLUE}[install]${NC} $1"; }
ok() { echo -e "${GREEN}[ok]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
err() { echo -e "${RED}[error]${NC} $1"; }
header() { echo -e "\n${BOLD}${CYAN}── $1 ──${NC}\n"; }

SKIP_DOCKER=false
SKIP_FRONTEND=false
DEV_MODE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-docker) SKIP_DOCKER=true; shift ;;
        --skip-frontend) SKIP_FRONTEND=true; shift ;;
        --dev) DEV_MODE=true; shift ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── 1. Check prerequisites ────────────────────────────────────────

header "Checking prerequisites"

check_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        ok "$1 found: $(command -v "$1")"
        return 0
    else
        err "$1 not found"
        return 1
    fi
}

MISSING=0

# Python
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
        ok "Python $PY_VER"
    else
        err "Python $PY_VER found, but >= $PYTHON_MIN_VERSION required"
        MISSING=1
    fi
else
    err "Python 3 not found"
    MISSING=1
fi

# Git
check_cmd git || MISSING=1

# Docker (optional)
if [ "$SKIP_DOCKER" = false ]; then
    if command -v docker >/dev/null 2>&1; then
        ok "Docker found"
    else
        warn "Docker not found — Qdrant will need to be run manually"
    fi
fi

# Node.js (optional)
if [ "$SKIP_FRONTEND" = false ]; then
    if command -v node >/dev/null 2>&1; then
        ok "Node.js found: $(node --version)"
    else
        warn "Node.js not found — frontend dashboard will be skipped"
        SKIP_FRONTEND=true
    fi
fi

if [ "$MISSING" -eq 1 ]; then
    echo ""
    err "Missing required dependencies. Install them and re-run."
    exit 1
fi

# ─── 2. Python virtual environment ─────────────────────────────────

header "Python virtual environment"

VENV_DIR="$HEPHAESTUS_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    ok "Virtual environment exists at $VENV_DIR"
else
    log "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    ok "Created $VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# Upgrade pip
log "Upgrading pip..."
"$PIP" install --upgrade pip --quiet

# ─── 3. Install Python dependencies ───────────────────────────────

header "Python dependencies"

log "Installing requirements..."
"$PIP" install -r "$HEPHAESTUS_DIR/requirements.txt" --quiet
ok "Backend dependencies installed"

if [ "$DEV_MODE" = true ]; then
    log "Installing dev dependencies..."
    "$PIP" install pytest pytest-asyncio pytest-cov black flake8 mypy ipython --quiet
    ok "Dev dependencies installed"
fi

# ─── 4. Install heph CLI ──────────────────────────────────────────

header "heph CLI"

log "Installing heph package (editable)..."
"$PIP" install -e "$HEPHAESTUS_DIR" --quiet 2>/dev/null || {
    # Fallback: create a wrapper script if pip install -e fails
    warn "pip install -e failed, creating wrapper script..."
    HEPH_BIN="$VENV_DIR/bin/heph"
    cat > "$HEPH_BIN" << 'WRAPPER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec "$DIR/bin/python" -m src.cli.main "$@"
WRAPPER
    chmod +x "$HEPH_BIN"
}

# Verify heph works
if "$VENV_DIR/bin/heph" --version >/dev/null 2>&1; then
    ok "heph CLI installed: $($VENV_DIR/bin/heph --version)"
elif "$PYTHON" -m src.cli.main --version >/dev/null 2>&1; then
    ok "heph CLI available via: python -m src.cli.main"
else
    warn "heph CLI may not be on PATH — use: $PYTHON -m src.cli.main"
fi

# Add to PATH hint
if ! echo "$PATH" | grep -q "$VENV_DIR/bin"; then
    echo ""
    log "Add heph to your PATH:"
    echo -e "  ${BOLD}export PATH=\"$VENV_DIR/bin:\$PATH\"${NC}"
    echo ""
    log "Or add to your shell profile:"
    echo -e "  ${BOLD}echo 'export PATH=\"$VENV_DIR/bin:\$PATH\"' >> ~/.zshrc${NC}"
    echo ""
fi

# ─── 5. Environment file ──────────────────────────────────────────

header "Environment configuration"

ENV_FILE="$HEPHAESTUS_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    ok ".env file exists"
else
    log "Creating .env from template..."
    cat > "$ENV_FILE" << 'ENVTEMPLATE'
# Hephaestus Environment Configuration
# Uncomment and set the values you need.

# LLM Provider (openrouter, openai, anthropic, groq)
# LLM_PROVIDER=openrouter
# LLM_MODEL=xiaomi/mimo-v2.5
# OPENROUTER_API_KEY=sk-or-...

# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...

# Database
# DATABASE_PATH=./hephaestus.db

# Vector Store
# VECTOR_STORE_BACKEND=turbovec
# QDRANT_URL=http://localhost:6333

# Embedding
# EMBEDDING_BACKEND=fastembed

# MCP Server
# MCP_HOST=127.0.0.1
# MCP_PORT=8000

# LiteLLM Proxy (optional, for cost tracking)
# LITELLM_PROXY_URL=http://deneb-server:4000
# LITELLM_API_KEY=sk-...
# LITELLM_MASTER_KEY=sk-...
# LITELLM_COST_TRACKING=true
ENVTEMPLATE
    ok "Created .env — edit it with your API keys"
fi

# ─── 6. Initialize database ───────────────────────────────────────

header "Database initialization"

log "Initializing SQLite database..."
"$PYTHON" "$HEPHAESTUS_DIR/scripts/init_db.py" 2>/dev/null && ok "Database initialized" || warn "Database init failed (may already exist)"

# ─── 7. Qdrant vector store ───────────────────────────────────────

if [ "$SKIP_DOCKER" = false ]; then
    header "Qdrant vector store"

    if curl -s http://localhost:6333/ >/dev/null 2>&1; then
        ok "Qdrant already running on port 6333"
    elif command -v docker >/dev/null 2>&1; then
        log "Starting Qdrant container..."
        if docker ps -a --format '{{.Names}}' | grep -q '^qdrant$'; then
            docker start qdrant >/dev/null 2>&1 && ok "Started existing Qdrant container" || warn "Failed to start Qdrant"
        else
            docker run -d -p 6333:6333 --name qdrant qdrant/qdrant >/dev/null 2>&1 && ok "Created Qdrant container" || warn "Failed to create Qdrant container"
        fi

        # Wait for Qdrant
        for i in $(seq 1 10); do
            sleep 1
            if curl -s http://localhost:6333/ >/dev/null 2>&1; then
                ok "Qdrant ready"
                break
            fi
        done
    else
        warn "Docker not available — start Qdrant manually or use VECTOR_STORE_BACKEND=turbovec"
    fi

    log "Initializing Qdrant collections..."
    "$PYTHON" "$HEPHAESTUS_DIR/scripts/init_qdrant.py" 2>/dev/null && ok "Qdrant collections initialized" || warn "Qdrant init failed (Qdrant may not be running)"
fi

# ─── 8. Frontend dashboard ────────────────────────────────────────

if [ "$SKIP_FRONTEND" = false ]; then
    header "Frontend dashboard"

    FRONTEND_DIR="$HEPHAESTUS_DIR/frontend"

    if [ -f "$FRONTEND_DIR/package.json" ]; then
        log "Installing frontend dependencies..."
        cd "$FRONTEND_DIR"
        npm install --silent 2>/dev/null && ok "Frontend dependencies installed" || warn "npm install failed"
        cd "$HEPHAESTUS_DIR"
    else
        warn "Frontend package.json not found — skipping"
    fi
fi

# ─── 9. Verify installation ───────────────────────────────────────

header "Verification"

echo ""
echo -e "${BOLD}Installed components:${NC}"
echo ""

# Python
echo -n "  Python:    "
"$PYTHON" --version 2>&1

# heph CLI
echo -n "  heph:      "
if "$VENV_DIR/bin/heph" --version 2>&1; then
    :
elif "$PYTHON" -m src.cli.main --version 2>&1; then
    :
fi

# Backend
echo -n "  Backend:   "
if curl -s http://localhost:8000/health >/dev/null 2>&1; then
    echo -e "${GREEN}running${NC}"
else
    echo -e "${YELLOW}not running${NC} (start with: heph start)"
fi

# Qdrant
echo -n "  Qdrant:    "
if curl -s http://localhost:6333/ >/dev/null 2>&1; then
    echo -e "${GREEN}running${NC}"
else
    echo -e "${YELLOW}not running${NC}"
fi

# Frontend
echo -n "  Frontend:  "
if [ -d "$HEPHAESTUS_DIR/frontend/node_modules" ]; then
    echo -e "${GREEN}installed${NC} (start with: heph start)"
else
    echo -e "${YELLOW}not installed${NC}"
fi

echo ""
echo -e "${BOLD}Quick start:${NC}"
echo ""
echo "  # Add to PATH (add to ~/.zshrc for persistence):"
echo "  export PATH=\"$VENV_DIR/bin:\$PATH\""
echo ""
echo "  # Start all services:"
echo "  heph start"
echo ""
echo "  # Check status:"
echo "  heph status"
echo ""
echo "  # Run autopilot:"
echo "  heph autopilot start --project-path ~/my-project"
echo ""
echo "  # Get help:"
echo "  heph --help"
echo ""

header "Done"
echo -e "${GREEN}Hephaestus is installed.${NC}"
echo ""
