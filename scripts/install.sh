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
#     curl -sSL https://raw.githubusercontent.com/hmuhlestein/HephaestusNG/main/scripts/install.sh | bash
#     curl -sSL https://raw.githubusercontent.com/hmuhlestein/HephaestusNG/main/scripts/install.sh | bash -s -- --dev
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
REPO_URL="https://github.com/hmuhlestein/HephaestusNG.git"
RAW_URL="https://raw.githubusercontent.com/hmuhlestein/HephaestusNG/main"

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
            echo "  curl -sSL $RAW_URL/scripts/install.sh | bash"
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

# uv (optional, faster installs)
if command -v uv >/dev/null 2>&1; then
    ok "uv: $(uv --version 2>&1)"
fi

# Git
if command -v git >/dev/null 2>&1; then
    ok "Git: $(command -v git)"
else
    err "Git not found. Install git and re-run."
    MISSING=1
fi

# Docker (only needed for qdrant)
if [ "$SKIP_DOCKER" = false ]; then
    VECTOR_BACKEND="${VECTOR_STORE_BACKEND:-turbovec}"
    if [ "$VECTOR_BACKEND" = "qdrant" ]; then
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
            ok "Docker: running"
        elif command -v docker >/dev/null 2>&1; then
            warn "Docker: installed but daemon not running"
        else
            warn "Docker not found — Qdrant requires Docker (or set VECTOR_STORE_BACKEND=turbovec)"
        fi
    else
        ok "Vector store: turbovec (Docker not needed)"
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

# Detect package manager: uv (preferred) > poetry > pip
PKG_MGR=""
if command -v uv >/dev/null 2>&1; then
    PKG_MGR="uv"
    ok "Package manager: uv ($(uv --version 2>&1))"
elif command -v poetry >/dev/null 2>&1; then
    PKG_MGR="poetry"
    ok "Package manager: poetry ($(poetry --version 2>&1))"
else
    PKG_MGR="pip"
    warn "Using pip (install uv for faster installs: https://docs.astral.sh/uv/)"
fi

if [ -d "$VENV_DIR" ] && [ "$UPDATE" = false ]; then
    # Validate existing venv: check Python version matches
    VENV_PYTHON="$VENV_DIR/bin/python"
    if [ -x "$VENV_PYTHON" ]; then
        VENV_PY_VER=$("$VENV_PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "unknown")
        SYSTEM_PY_VER=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "unknown")
        if [ "$VENV_PY_VER" = "$SYSTEM_PY_VER" ]; then
            ok "Virtual environment exists (Python $VENV_PY_VER)"
        else
            warn "Virtual environment Python mismatch: venv=$VENV_PY_VER, system=$SYSTEM_PY_VER"
            log "Recreating virtual environment..."
            rm -rf "$VENV_DIR"
            if [ "$PKG_MGR" = "uv" ]; then
                uv venv "$VENV_DIR" --python "$PYTHON_CMD" --quiet
            else
                "$PYTHON_CMD" -m venv "$VENV_DIR"
            fi
            ok "Recreated with Python $SYSTEM_PY_VER"
        fi
    else
        warn "Virtual environment exists but Python not executable"
        log "Recreating virtual environment..."
        rm -rf "$VENV_DIR"
        if [ "$PKG_MGR" = "uv" ]; then
            uv venv "$VENV_DIR" --python "$PYTHON_CMD" --quiet
        else
            "$PYTHON_CMD" -m venv "$VENV_DIR"
        fi
        ok "Recreated virtual environment"
    fi
else
    log "Creating virtual environment..."
    if [ "$PKG_MGR" = "uv" ]; then
        "$UV" venv "$VENV_DIR" --python "$PYTHON_CMD" --quiet
    elif [ "$PKG_MGR" = "poetry" ]; then
        poetry env use "$PYTHON_CMD" --directory "$PREFIX" 2>/dev/null || "$PYTHON_CMD" -m venv "$VENV_DIR"
    else
        "$PYTHON_CMD" -m venv "$VENV_DIR"
    fi
    ok "Created $VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"

# ─── 4. Python dependencies ───────────────────────────────────────

header "Dependencies"

# Check if core deps are already installed
if "$PYTHON" -c "import fastapi, uvicorn, sqlalchemy" 2>/dev/null; then
    ok "Backend dependencies already installed"
else
    log "Installing requirements..."
    if [ "$PKG_MGR" = "uv" ]; then
        uv pip install -r "$PREFIX/requirements.txt" --quiet --python "$PYTHON"
    elif [ "$PKG_MGR" = "poetry" ]; then
        cd "$PREFIX" && poetry install --no-interaction --quiet 2>&1 | tail -3
    else
        "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
        "$VENV_DIR/bin/pip" install -r "$PREFIX/requirements.txt" --quiet 2>&1 | tail -1
    fi
    ok "Backend dependencies installed"
fi

if [ "$DEV_MODE" = true ]; then
    if "$PYTHON" -c "import pytest" 2>/dev/null; then
        ok "Dev dependencies already installed"
    else
        log "Installing dev dependencies..."
        if [ "$PKG_MGR" = "uv" ]; then
            uv pip install pytest pytest-asyncio pytest-cov black flake8 mypy ipython --quiet --python "$PYTHON"
        elif [ "$PKG_MGR" = "poetry" ]; then
            cd "$PREFIX" && poetry install --with dev --no-interaction --quiet 2>&1 | tail -3
        else
            "$VENV_DIR/bin/pip" install pytest pytest-asyncio pytest-cov black flake8 mypy ipython --quiet
        fi
        ok "Dev dependencies installed"
    fi
fi

# ─── 5. heph CLI ──────────────────────────────────────────────────

header "heph CLI"

HEPH_BIN="$VENV_DIR/bin/heph"

if [ -x "$HEPH_BIN" ] && "$HEPH_BIN" --version >/dev/null 2>&1; then
    ok "heph already installed: $("$HEPH_BIN" --version 2>&1)"
else
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

DB_FILE="$PREFIX/hephaestus.db"
if [ -f "$DB_FILE" ]; then
    ok "Database exists ($(du -h "$DB_FILE" | cut -f1))"
else
    log "Initializing SQLite..."
    "$PYTHON" "$PREFIX/scripts/init_db.py" 2>/dev/null && ok "Database ready" || warn "Database init failed"
fi

# ─── 8. Qdrant (only if configured as backend) ─────────────────

VECTOR_BACKEND="${VECTOR_STORE_BACKEND:-turbovec}"
if [ "$VECTOR_BACKEND" = "qdrant" ] && [ "$SKIP_DOCKER" = false ]; then
    header "Qdrant"

    if curl -s http://localhost:6333/ >/dev/null 2>&1; then
        ok "Qdrant already running"
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
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
    else
        warn "Qdrant configured but Docker not available — set VECTOR_STORE_BACKEND=turbovec or start Docker"
    fi

    "$PYTHON" "$PREFIX/scripts/init_qdrant.py" 2>/dev/null && ok "Collections initialized" || warn "Qdrant init skipped"
else
    ok "Vector store: turbovec (no Docker needed)"
fi

# ─── 9. Frontend ──────────────────────────────────────────────────

if [ "$SKIP_FRONTEND" = false ]; then
    header "Frontend"

    FRONTEND_DIR="$PREFIX/frontend"
    if [ -f "$FRONTEND_DIR/package.json" ]; then
        if [ -d "$FRONTEND_DIR/node_modules" ] && [ "$UPDATE" = false ]; then
            ok "Frontend dependencies already installed"
        else
            log "Installing frontend deps..."
            cd "$FRONTEND_DIR"
            npm install --silent 2>/dev/null && ok "Frontend ready" || warn "npm install failed"
            cd "$PREFIX"
        fi
    else
        warn "Frontend package.json not found — skipping"
    fi
fi

# ─── 10. Verify ───────────────────────────────────────────────────

header "Installation complete"

echo ""
echo -e "${BOLD}Location:${NC}  $PREFIX"
echo -e "${BOLD}heph:${NC}      $HEPH_BIN"
echo ""

# Add to PATH
case ":$PATH:" in
    *":$VENV_DIR/bin:"*) ok "heph is on PATH" ;;
    *)
        # Detect shell profile
        SHELL_NAME="$(basename "$SHELL")"
        case "$SHELL_NAME" in
            zsh) PROFILE="$HOME/.zshrc" ;;
            bash) PROFILE="$HOME/.bashrc" ;;
            *) PROFILE="" ;;
        esac

        PATH_LINE="export PATH=\"$VENV_DIR/bin:\$PATH\""

        if [ -n "$PROFILE" ] && [ -f "$PROFILE" ]; then
            if grep -qF "$VENV_DIR/bin" "$PROFILE" 2>/dev/null; then
                ok "heph already in $PROFILE"
            else
                echo "" >> "$PROFILE"
                echo "# Hephaestus heph CLI" >> "$PROFILE"
                echo "$PATH_LINE" >> "$PROFILE"
                ok "Added heph to $PROFILE"
            fi
        else
            echo ""
            echo -e "${BOLD}Add heph to PATH:${NC}"
            echo ""
            echo -e "  ${CYAN}$PATH_LINE${NC}"
            echo ""
            echo -e "Add to your shell profile for persistence."
            echo ""
        fi

        # Export for current session
        export PATH="$VENV_DIR/bin:$PATH"
        ;;
esac

echo -e "${BOLD}Quick start:${NC}"
echo ""
echo "  heph project setup <name> <path>  # Create and activate a project"
echo "  heph status                       # Check system health"
echo "  heph start                        # Start all services"
echo "  heph exec test                    # Test service connectivity"
echo "  heph workflow list                 # List workflow definitions"
echo "  heph autopilot --help             # Autopilot pipeline"
echo "  heph --help                       # All commands"
echo ""

if [ "$LOCAL_MODE" = false ]; then
    echo -e "${BOLD}Project directory:${NC}"
    echo ""
    echo "  $PREFIX"
    echo ""
fi

header "Done"
