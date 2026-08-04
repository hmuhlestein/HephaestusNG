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

# timeout (coreutils) - used for agent command timeouts
if command -v timeout >/dev/null 2>&1; then
    ok "timeout: $(command -v timeout)"
else
    warn "timeout not found (part of coreutils) — agent timeouts will not work"
    warn "Install with: brew install coreutils (macOS) or apt install coreutils (Linux)"
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

# Install package in editable mode (creates heph entry point)
if "$PYTHON" -c "from src.cli.main import main" 2>/dev/null; then
    ok "Package already installed"
else
    log "Installing package in editable mode..."
    if [ "$PKG_MGR" = "uv" ]; then
        uv pip install -e "$PREFIX" --quiet --python "$PYTHON" 2>&1 | tail -3
    elif [ "$PKG_MGR" = "poetry" ]; then
        cd "$PREFIX" && poetry install --no-interaction --quiet 2>&1 | tail -3
    else
        "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
        "$VENV_DIR/bin/pip" install -e "$PREFIX" --quiet 2>&1 | tail -1
    fi
    ok "Package installed (heph entry point created)"
fi

if [ "$DEV_MODE" = true ]; then
    if "$PYTHON" -c "import pytest" 2>/dev/null; then
        ok "Dev dependencies already installed"
    else
        log "Installing dev dependencies..."
        if [ "$PKG_MGR" = "uv" ]; then
            uv pip install "pytest,pytest-asyncio,pytest-cov,black,flake8,mypy,ruff,ipython" --quiet --python "$PYTHON"
        elif [ "$PKG_MGR" = "poetry" ]; then
            cd "$PREFIX" && poetry install --with dev --no-interaction --quiet 2>&1 | tail -3
        else
            "$VENV_DIR/bin/pip" install pytest pytest-asyncio pytest-cov black flake8 mypy ruff ipython --quiet
        fi
        ok "Dev dependencies installed"
    fi
fi

# ─── 4.5. Embedding model (pre-download) ──────────────────────────
# FastEmbed downloads its model (~90MB) on first use, which otherwise causes a
# multi-minute stall the first time the pipeline generates an embedding. Warm it
# up here so the first run is fast.

header "Embedding model"

EMBED_BACKEND="${EMBEDDING_BACKEND:-fastembed}"
if [ "$EMBED_BACKEND" = "fastembed" ]; then
    FE_MODEL="${FASTEMBED_MODEL:-BAAI/bge-small-en-v1.5}"
    if "$PYTHON" -c "
from langchain_community.embeddings import FastEmbedEmbeddings
import sys
# Cached models load instantly; first run downloads ~90MB.
sys.stderr.write('  downloading/caching $FE_MODEL (one-time)...\n')
FastEmbedEmbeddings(model_name='$FE_MODEL').embed_query('warmup')
" 2>/dev/null; then
        ok "Embedding model cached ($FE_MODEL)"
    else
        warn "FastEmbed pre-download failed — it will download on first use (one-time delay)"
    fi
else
    ok "Embedding backend: $EMBED_BACKEND (no pre-download needed)"
fi

# ─── 4.7. Security tools ──────────────────────────────────────────
# ash (AWS automated-security-helper) via uvx — local mode, no Docker required.
# https://github.com/awslabs/automated-security-helper

header "Security tools"

ASH_WRAPPER="$VENV_DIR/bin/ash"
if [ -x "$ASH_WRAPPER" ]; then
    ok "ash wrapper already installed"
else
    _install_ash=true
    if [ -t 0 ]; then
        printf "${BLUE}[heph]${NC} Install AWS automated-security-helper (ash) for the security review phase? [Y/n] "
        read -r _ash_reply </dev/tty
        case "${_ash_reply:-Y}" in
            [Nn]*) _install_ash=false ;;
        esac
    fi

    if $_install_ash; then
        log "Installing ash wrapper (uvx local mode)..."
        cp "$PREFIX/scripts/ash" "$ASH_WRAPPER" \
          && chmod +x "$ASH_WRAPPER" \
          && ok "ash wrapper installed → $ASH_WRAPPER" \
          || warn "ash wrapper install failed — security phase will skip automated scan"
    else
        ok "ash skipped — security phase will note it as unavailable"
    fi
fi

# ─── 5. heph CLI ──────────────────────────────────────────────────

header "heph CLI"

HEPH_BIN="$VENV_DIR/bin/heph"

if [ -x "$HEPH_BIN" ] && "$HEPH_BIN" --version >/dev/null 2>&1; then
    ok "heph already installed: $("$HEPH_BIN" --version 2>&1)"
else
    warn "heph CLI not found after install — try: uv pip install -e ."
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

# ─── 10. Pi Extension (Cost Tracker) ─────────────────────────────

header "Pi Extension (Cost Tracker)"

EXT_SRC="$PREFIX/extensions/hephaestus-cost-tracker"
PI_EXT_DIR="$HOME/.pi/agent/extensions"

if [ -d "$EXT_SRC" ]; then
    # Build the extension if needed
    if [ -f "$EXT_SRC/package.json" ]; then
        log "Building cost tracker extension..."
        cd "$EXT_SRC"
        if [ ! -d "node_modules" ]; then
            npm install --silent 2>/dev/null
        fi
        npx tsc 2>/dev/null && ok "Extension built" || warn "Extension build failed (npm/npx required)"
        cd "$PREFIX"
    fi

    # Symlink into pi's global extensions directory.
    # Pi auto-discovers *.ts files -- the compiled .js is NOT discovered.
    # Symlink the source .ts so pi can load it directly (pi runs TS natively).
    if [ -f "$EXT_SRC/src/index.ts" ]; then
        mkdir -p "$PI_EXT_DIR"
        ln -sf "$EXT_SRC/src/index.ts" "$PI_EXT_DIR/hephaestus-cost-tracker.ts"
        ok "Cost tracker symlinked → $PI_EXT_DIR/hephaestus-cost-tracker.ts"
    elif [ -f "$EXT_SRC/dist/index.js" ]; then
        mkdir -p "$PI_EXT_DIR"
        ln -sf "$EXT_SRC/dist/index.js" "$PI_EXT_DIR/hephaestus-cost-tracker.js"
        ok "Cost tracker symlinked → $PI_EXT_DIR/hephaestus-cost-tracker.js"
    else
        warn "Extension source/dist not found — build may have failed"
    fi
else
    warn "Extension source not found at $EXT_SRC — skipping"
fi

# ─── 11. Update Model Pricing ──────────────────────────────────────

log "Updating model pricing from OpenRouter API..."
python3 "$PREFIX/scripts/update_model_pricing.py" 2>/dev/null && ok "Model pricing updated" || warn "Could not update pricing (using defaults)"

# ─── 12. Verify ───────────────────────────────────────────────────

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

# ─── OpenCode MCP Setup ──────────────────────────────────────────

header "OpenCode MCP Configuration"

OPENCODE_CONFIG="$HOME/.config/opencode/opencode.jsonc"
mkdir -p "$(dirname "$OPENCODE_CONFIG")"

# Determine python path
if [ -n "$VENV_DIR" ]; then
    PYTHON_PATH="$VENV_DIR/bin/python"
else
    PYTHON_PATH="$(which python3)"
fi

# Determine script path
if [ "$LOCAL_MODE" = true ]; then
    MCP_SCRIPT="$REPO_DIR/mcp/mcp_client.py"
else
    MCP_SCRIPT="$PREFIX/mcp/mcp_client.py"
fi

# Write opencode config if it doesn't exist or is outdated
if [ ! -f "$OPENCODE_CONFIG" ] || ! grep -q "hephaestusNG" "$OPENCODE_CONFIG" 2>/dev/null; then
    cat > "$OPENCODE_CONFIG" << OPEOF
{
  "\$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hephaestus": {
      "type": "local",
      "command": ["$PYTHON_PATH", "$MCP_SCRIPT"]
    }
  }
}
OPEOF
    ok "Wrote $OPENCODE_CONFIG"
else
    ok "OpenCode config already up to date"
fi

# ─── Pi Agent MCP Configuration ──────────────────────────────────

header "Pi Agent MCP Configuration"

PI_AGENTS_DIR="$HOME/.pi/agent/agents"
PI_MCP_CONFIG="$HOME/.config/mcp/mcp.json"
PI_MCP_BACKUP="$HOME/.config/mcp/mcp.json.bak"

# Check if pi CLI is installed (must be on PATH — just having ~/.pi is not enough)
if command -v pi >/dev/null 2>&1; then
    log "Pi detected — configuring MCP tools"
    
    # Check if pi-mcp-adapter is installed (wraps MCP servers for pi without context overhead)
    if ! pi list 2>/dev/null | grep -q 'pi-mcp-adapter'; then
        log "Installing pi-mcp-adapter..."
        if pi install npm:pi-mcp-adapter 2>&1 | tail -3; then
            ok "pi-mcp-adapter installed via pi"
        elif command -v npm >/dev/null 2>&1 && npm install -g pi-mcp-adapter 2>&1 | tail -3; then
            ok "pi-mcp-adapter installed via npm"
        else
            warn "Failed to install pi-mcp-adapter — MCP tools may not work"
            warn "Install manually: pi install npm:pi-mcp-adapter"
        fi
    else
        ok "pi-mcp-adapter already installed"
    fi

    # Offer to install pi-codegraph-extension
    if ! pi list 2>/dev/null | grep -q 'pi-codegraph-extension'; then
        _install_codegraph=true
        if [ -t 0 ]; then
            printf "${BLUE}[heph]${NC} Install pi-codegraph-extension (codebase indexing for pi)? [Y/n] "
            read -r _cg_reply </dev/tty
            case "${_cg_reply:-Y}" in
                [Nn]*) _install_codegraph=false ;;
            esac
        fi
        if $_install_codegraph; then
            log "Installing pi-codegraph-extension..."
            if pi install npm:pi-codegraph-extension 2>&1 | tail -3; then
                ok "pi-codegraph-extension installed"
            else
                warn "Failed to install pi-codegraph-extension"
                warn "Install manually: pi install npm:pi-codegraph-extension"
            fi
        else
            ok "pi-codegraph-extension skipped"
        fi
    else
        ok "pi-codegraph-extension already installed"
    fi
    
    # Generate and install Hephaestus pi agents from phase files
    log "Generating Hephaestus pi agents from phase definitions..."
    if [ -f "$PREFIX/scripts/generate_pi_agents.py" ]; then
        "$PYTHON_PATH" "$PREFIX/scripts/generate_pi_agents.py" 2>/dev/null
        if [ $? -eq 0 ]; then
            # Copy generated agents to pi agents directory
            mkdir -p "$PI_AGENTS_DIR"
            if [ -d "$PREFIX/agents/pi" ]; then
                cp "$PREFIX/agents/pi"/*.md "$PI_AGENTS_DIR" 2>/dev/null
                agent_count=$(ls -1 "$PREFIX/agents/pi"/*.md 2>/dev/null | wc -l)
                ok "Installed $agent_count Hephaestus pi agents"
            fi
        else
            warn "Failed to generate pi agents"
        fi
    else
        warn "generate_pi_agents.py not found — skipping agent generation"
    fi
    
    # Create MCP config directory
    mkdir -p "$(dirname "$PI_MCP_CONFIG")"
    
    # Determine python path for MCP server
    if [ -n "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
        MCP_PYTHON="$VENV_DIR/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        MCP_PYTHON="$(which python3)"
    else
        warn "Python not found — cannot configure MCP"
        MCP_PYTHON=""
    fi
    
    # Determine MCP script path
    if [ "$LOCAL_MODE" = true ]; then
        MCP_SCRIPT_PATH="$PREFIX/mcp/mcp_client.py"
    else
        MCP_SCRIPT_PATH="$PREFIX/mcp/mcp_client.py"
    fi
    
    # Verify MCP script exists
    if [ ! -f "$MCP_SCRIPT_PATH" ]; then
        warn "MCP script not found at $MCP_SCRIPT_PATH — skipping MCP config"
        MCP_SCRIPT_PATH=""
    fi
    
    # Configure MCP if we have python and script
    if [ -n "$MCP_PYTHON" ] && [ -n "$MCP_SCRIPT_PATH" ]; then
        # Backup existing config
        if [ -f "$PI_MCP_CONFIG" ]; then
            cp "$PI_MCP_CONFIG" "$PI_MCP_BACKUP" 2>/dev/null
        fi
        
        # Write MCP config if it doesn't exist, or merge if it does
        if [ ! -f "$PI_MCP_CONFIG" ]; then
            cat > "$PI_MCP_CONFIG" << MCPEOF
{
  "mcpServers": {
    "hephaestus": {
      "command": "$MCP_PYTHON",
      "args": ["$MCP_SCRIPT_PATH"]
    }
  }
}
MCPEOF
            if [ $? -eq 0 ]; then
                ok "Wrote $PI_MCP_CONFIG"
            else
                warn "Failed to write MCP config"
            fi
        else
            # Check if hephaestus is already configured
            if grep -q '"hephaestus"' "$PI_MCP_CONFIG" 2>/dev/null; then
                ok "Hephaestus MCP already configured"
            else
                # Add hephaestus to existing config using python for safe JSON merge
                log "Adding hephaestus to existing MCP config..."
                "$MCP_PYTHON" -c "
import json
import sys
import os

config_path = '$PI_MCP_CONFIG'
backup_path = '$PI_MCP_BACKUP'

try:
    # Read existing config
    with open(config_path, 'r') as f:
        content = f.read().strip()
        if content:
            config = json.loads(content)
        else:
            config = {}
except (json.JSONDecodeError, FileNotFoundError) as e:
    # If backup exists, restore it
    if os.path.exists(backup_path):
        with open(backup_path, 'r') as f:
            config = json.load(f)
    else:
        config = {}

# Ensure mcpServers exists
if 'mcpServers' not in config:
    config['mcpServers'] = {}

# Add hephaestus server
config['mcpServers']['hephaestus'] = {
    'command': '$MCP_PYTHON',
    'args': ['$MCP_SCRIPT_PATH']
}

# Write updated config
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)

print('OK')
" 2>/dev/null
                if [ $? -eq 0 ]; then
                    ok "Added hephaestus to MCP config"
                else
                    warn "Failed to update MCP config"
                    # Restore backup if it exists
                    if [ -f "$PI_MCP_BACKUP" ]; then
                        cp "$PI_MCP_BACKUP" "$PI_MCP_CONFIG" 2>/dev/null
                        log "Restored backup config"
                    fi
                fi
            fi
        fi
    fi
    
    # Add MCP tools to pi agent files if they exist
    mkdir -p "$PI_AGENTS_DIR"
    
    # MCP tools to add to pi agents
    MCP_TOOLS="mcp:hephaestus/save_memory, mcp:hephaestus/search_memory, mcp:hephaestus/get_task_status, mcp:hephaestus/create_task, mcp:hephaestus/update_task_status"
    
    # Function to add MCP tools to agent file
    add_mcp_tools() {
        local agent_file="$1"
        local agent_name="$2"
        
        if [ ! -f "$agent_file" ]; then
            log "$agent_name not found — skipping"
            return
        fi
        
        # Check if already has MCP tools
        if grep -q 'mcp:hephaestus' "$agent_file" 2>/dev/null; then
            ok "$agent_name already has MCP tools"
            return
        fi
        
        # Backup the agent file
        cp "$agent_file" "${agent_file}.bak" 2>/dev/null
        
        # Try to add MCP tools to tools line
        if grep -q '^tools:' "$agent_file" 2>/dev/null; then
            # Check if tools line is the simple format we expect
            if grep -q '^tools: read, write, edit, bash, grep, find, ls$' "$agent_file" 2>/dev/null; then
                sed -i '' "s|^tools: read, write, edit, bash, grep, find, ls$|tools: read, write, edit, bash, grep, find, ls, $MCP_TOOLS|" "$agent_file" 2>/dev/null
            else
                # Tools line has custom content, append MCP tools
                sed -i '' "s|^tools: |tools: | $MCP_TOOLS|" "$agent_file" 2>/dev/null
            fi
            
            if [ $? -eq 0 ]; then
                ok "Added MCP tools to $agent_name"
            else
                warn "Failed to update $agent_name"
                # Restore backup
                if [ -f "${agent_file}.bak" ]; then
                    cp "${agent_file}.bak" "$agent_file" 2>/dev/null
                fi
            fi
        else
            log "$agent_name has no tools line — skipping"
        fi
    }
    
    # Update pi-developer agent
    add_mcp_tools "$PI_AGENTS_DIR/pi-developer.md" "pi-developer"
    
    # Update pi-adversarial-review agent
    add_mcp_tools "$PI_AGENTS_DIR/pi-adversarial-review.md" "pi-adversarial-review"
    
    # Update pi-qa agent
    add_mcp_tools "$PI_AGENTS_DIR/pi-qa.md" "pi-qa"
    
    # Update pi-researcher agent
    add_mcp_tools "$PI_AGENTS_DIR/pi-researcher.md" "pi-researcher"
    
    # Cleanup backup files
    rm -f "$PI_MCP_BACKUP" 2>/dev/null
    rm -f "$PI_AGENTS_DIR"/*.bak 2>/dev/null

    log "Restart Pi after installation for MCP tools to take effect"
    log "pi-mcp-adapter reads from ~/.config/mcp/mcp.json automatically"
    
else
    log "Pi not detected — skipping MCP tool configuration"
    log "To configure later:"
    log "  1. Install pi-mcp-adapter: pi install npm:pi-mcp-adapter"
    log "  2. Create $PI_MCP_CONFIG with your MCP server configuration"
fi

header "Done"
