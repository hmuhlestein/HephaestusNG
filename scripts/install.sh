#!/bin/bash
#
# Hephaestus Install Script
#
# Works in two modes:
#
#   Local dev (from cloned repo):
#     ./install.sh
#
#   Remote install (no repo needed):
#     curl -sSL https://raw.githubusercontent.com/hmuhlestein/Hephaestus/main/install.sh | bash
#     curl -sSL https://raw.githubusercontent.com/hmuhlestein/Hephaestus/main/install.sh | bash -s -- --dev
#
# Flags:
#   --prefix DIR        Install location (default: ~/.hephaestus)
#   --skip-docker       Skip Docker/Qdrant setup
#   --skip-frontend     Skip frontend dashboard
#   --dev               Install dev dependencies (pytest, black, etc.)
#   --update            Pull latest and reinstall
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${BLUE}[heph]${NC} $1"; }
ok() { echo -e "${GREEN}[ok]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
err() { echo -e "${RED}[error]${NC} $1"; }
header() { echo -e "\n${BOLD}${CYAN}── $1 ──${NC}\n"; }

PYTHON_MIN_VERSION="311"
REPO_URL="https://github.com/hmuhlestein/Hephaestus.git"
RAW_URL="https://raw.githubusercontent.com/hmuhlestein/Hephaestus/main"

# ─── Parse arguments ───────────────────────────────────────────────

PREFIX="${HEPHAESTUS_HOME:-$HOME/.hephaestus}"
SKIP_DOCKER=false
SKIP_FRONTEND=false
DEV_MODE=false
UPDATE=false
LOCAL_MODE=false

# Detect local dev mode: if we're inside the repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd)"
# If running from scripts/, check parent for repo indicators
if [ -f "$SCRIPT_DIR/../src/cli/main.py" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    LOCAL_MODE=true
    PREFIX="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$SCRIPT_DIR/src/cli/main.py" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    LOCAL_MODE=true
    PREFIX="$SCRIPT_DIR"
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        --prefix) PREFIX="$2"; shift 2 ;;
        --skip-docker) SKIP_DOCKER=true; shift ;;
        --skip-frontend) SKIP_FRONTEND=true; shift ;;
        --dev) DEV_MODE=true; shift ;;
        --update) UPDATE=true; shift ;;
        -h|--help)
            echo "Usage: install.sh [options]"
            echo ""
            echo "Options:"
            echo "  --prefix DIR        Install location (default: ~/.hephaestus)"
            echo "  --skip-docker       Skip Docker/Qdrant setup"
            echo "  --skip-frontend     Skip frontend dashboard"
            echo "  --dev               Install dev dependencies"
            echo "  --update            Pull latest and reinstall"
            echo "  -h, --help          Show this help"
            echo ""
            echo "Remote install:"
            echo "  curl -sSL $RAW_URL/install.sh | bash"
            echo ""
            echo "Local install (from cloned repo):"
            echo "  ./install.sh"
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── 1. Check prerequisites ────────────────────────────────────────

header "Checking prerequisites"

MISSING=0

# Python
find_python() {
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor:02d}')" 2>/dev/null || echo "0")
            if [ "$ver" -ge "$PYTHON_MIN_VERSION" ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_CMD=$(find_python) || {
    err "Python >= 3.11 required but not found."
    err "Install Python 3.11+ and re-run."
    exit 1
}
ok "Python: $("$PYTHON_CMD" --version 2>&1)"

# Git
if command -v git >/dev/null 2>&1; then
    ok "Git: $(command -v git)"
else
    err "Git not found. Install git and re-run."
    MISSING=1
fi

# Docker (optional)
if [ "$SKIP_DOCKER" = false ]; then
    if command -v docker >/dev/null 2>&1; then
        ok "Docker: $(command -v docker)"
    else
        warn "Docker not found — Qdrant will need manual setup"
    fi
fi

# Node.js (optional)
if [ "$SKIP_FRONTEND" = false ]; then
    if command -v node >/dev/null 2>&1; then
        ok "Node.js: $(node --version 2>&1)"
    else
        warn "Node.js not found — skipping frontend dashboard"
        SKIP_FRONTEND=true
    fi
fi

if [ "$MISSING" -eq 1 ]; then
    exit 1
fi

# ─── 2. Get the code ──────────────────────────────────────────────

header "Repository"

if [ "$LOCAL_MODE" = true ]; then
    ok "Local dev mode: $PREFIX"
    cd "$PREFIX"
    if [ "$UPDATE" = true ]; then
        log "Pulling latest..."
        git pull --quiet
        ok "Updated"
    fi
else
    if [ -d "$PREFIX/.git" ]; then
        ok "Existing install at $PREFIX"
        cd "$PREFIX"
        if [ "$UPDATE" = true ]; then
            log "Pulling latest..."
            git pull --quiet
            ok "Updated"
        fi
    else
        log "Cloning Hephaestus to $PREFIX..."
        git clone --quiet "$REPO_URL" "$PREFIX"
        cd "$PREFIX"
        ok "Cloned to $PREFIX"
    fi
fi

# ─── 3. Python virtual environment ─────────────────────────────────

header "Python environment"

VENV_DIR="$PREFIX/.venv"

if [ -d "$VENV_DIR" ] && [ "$UPDATE" = false ]; then
    ok "Virtual environment exists"
else
    log "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "Created $VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

log "Upgrading pip..."
"$PIP" install --upgrade pip --quiet 2>/dev/null

# ─── 4. Python dependencies ───────────────────────────────────────

header "Dependencies"

log "Installing requirements..."
"$PIP" install -r "$PREFIX/requirements.txt" --quiet 2>&1 | tail -1
ok "Backend dependencies installed"

if [ "$DEV_MODE" = true ]; then
    log "Installing dev dependencies..."
    "$PIP" install pytest pytest-asyncio pytest-cov black flake8 mypy ipython --quiet
    ok "Dev dependencies installed"
fi

# ─── 5. heph CLI ──────────────────────────────────────────────────

header "heph CLI"

HEPH_BIN="$VENV_DIR/bin/heph"

cat > "$HEPH_BIN" << WRAPPER
#!/bin/bash
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON" -m src.cli.main "\$@"
WRAPPER
chmod +x "$HEPH_BIN"

if "$HEPH_BIN" --version >/dev/null 2>&1; then
    ok "heph installed: $("$HEPH_BIN" --version 2>&1)"
else
    warn "heph CLI created but version check failed"
fi

# ─── 6. Environment file ──────────────────────────────────────────

header "Configuration"

ENV_FILE="$PREFIX/.env"

if [ -f "$ENV_FILE" ]; then
    ok ".env exists"
else
    cat > "$ENV_FILE" << 'ENVEOF'
# Hephaestus Configuration
# Uncomment and set values as needed.

# LLM Provider: openrouter, openai, anthropic, groq
# LLM_PROVIDER=openrouter
# LLM_MODEL=xiaomi/mimo-v2.5
# OPENROUTER_API_KEY=sk-or-...
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...

# Vector Store (turbovec is local, qdrant needs Docker)
# VECTOR_STORE_BACKEND=turbovec
# EMBEDDING_BACKEND=fastembed
# QDRANT_URL=http://localhost:6333

# Server
# MCP_HOST=127.0.0.1
# MCP_PORT=8000

# LiteLLM Proxy (optional, for cost tracking)
# LITELLM_PROXY_URL=http://deneb-server:4000
# LITELLM_API_KEY=sk-...
# LITELLM_MASTER_KEY=sk-...
# LITELLM_COST_TRACKING=true
ENVEOF
    ok "Created .env — edit with your API keys"
fi

# ─── 7. Database ──────────────────────────────────────────────────

header "Database"

log "Initializing SQLite..."
"$PYTHON" "$PREFIX/scripts/init_db.py" 2>/dev/null && ok "Database ready" || warn "Database init skipped (may already exist)"

# ─── 8. Qdrant ────────────────────────────────────────────────────

if [ "$SKIP_DOCKER" = false ]; then
    header "Qdrant"

    if curl -s http://localhost:6333/ >/dev/null 2>&1; then
        ok "Qdrant already running"
    elif command -v docker >/dev/null 2>&1; then
        log "Starting Qdrant..."
        if docker ps -a --format '{{.Names}}' | grep -q '^qdrant$'; then
            docker start qdrant >/dev/null 2>&1
        else
            docker run -d -p 6333:6333 --name qdrant qdrant/qdrant >/dev/null 2>&1
        fi
        sleep 3
        if curl -s http://localhost:6333/ >/dev/null 2>&1; then
            ok "Qdrant running"
        else
            warn "Qdrant may still be starting..."
        fi
    fi

    "$PYTHON" "$PREFIX/scripts/init_qdrant.py" 2>/dev/null && ok "Collections initialized" || warn "Qdrant init skipped"
fi

# ─── 9. Frontend ──────────────────────────────────────────────────

if [ "$SKIP_FRONTEND" = false ]; then
    header "Frontend"

    FRONTEND_DIR="$PREFIX/frontend"
    if [ -f "$FRONTEND_DIR/package.json" ]; then
        log "Installing frontend deps..."
        cd "$FRONTEND_DIR"
        npm install --silent 2>/dev/null && ok "Frontend ready" || warn "npm install failed"
        cd "$PREFIX"
    fi
fi

# ─── 10. Verify ───────────────────────────────────────────────────

header "Installation complete"

echo ""
echo -e "${BOLD}Location:${NC}  $PREFIX"
echo -e "${BOLD}heph:${NC}      $HEPH_BIN"
echo ""

# Add to PATH instruction
case ":$PATH:" in
    *":$VENV_DIR/bin:"*) ok "heph is on PATH" ;;
    *)
        echo -e "${BOLD}Add heph to PATH:${NC}"
        echo ""
        echo -e "  ${CYAN}export PATH=\"$VENV_DIR/bin:\$PATH\"${NC}"
        echo ""
        echo -e "Add to ~/.zshrc or ~/.bashrc for persistence."
        echo ""
        ;;
esac

echo -e "${BOLD}Quick start:${NC}"
echo ""
echo "  heph status              # Check system health"
echo "  heph start               # Start all services"
echo "  heph exec test           # Test service connectivity"
echo "  heph workflow list        # List workflow definitions"
echo "  heph autopilot --help    # Autopilot pipeline"
echo "  heph --help              # All commands"
echo ""

if [ "$LOCAL_MODE" = false ]; then
    echo -e "${BOLD}Project directory:${NC}"
    echo ""
    echo "  $PREFIX"
    echo ""
fi

header "Done"
