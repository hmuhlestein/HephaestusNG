#!/bin/bash
#
# Hephaestus Uninstall Script
#
# Removes Hephaestus agents from all configured CLI agent systems
# (pi, Claude Code, CodeGraph MCP) and optionally deletes the install.
#
# Usage:
#   ./uninstall.sh              # interactive — asks before each step
#   ./uninstall.sh --yes        # non-interactive — deletes everything
#   ./uninstall.sh --agents-only # remove agents only, keep install
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${BLUE}[heph]${NC} $1"; }
ok() { echo -e "${GREEN}[ok]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
err() { echo -e "${RED}[error]${NC} $1"; }
header() { echo -e "\n${BOLD}── $1 ──${NC}\n"; }

PREFIX="${HEPHAESTUS_HOME:-$HOME/.hephaestus}"
PI_AGENTS_DIR="$HOME/.pi/agent/agents"
CLAUDE_AGENTS_DIR="$HOME/.claude/agents"

AUTO_YES=false
AGENTS_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --yes|-y) AUTO_YES=true ;;
        --agents-only) AGENTS_ONLY=true ;;
        --prefix) shift; PREFIX="$1" ;;
        --help|-h)
            echo "Usage: uninstall.sh [--yes] [--agents-only] [--prefix DIR]"
            echo ""
            echo "  --yes          Skip all confirmation prompts"
            echo "  --agents-only  Remove agents only, keep the install directory"
            echo "  --prefix DIR   Hephaestus install location (default: ~/.hephaestus)"
            exit 0
            ;;
    esac
done

confirm() {
    if $AUTO_YES; then return 0; fi
    printf "${BLUE}[heph]${NC} $1 [Y/n] "
    read -r _reply </dev/tty
    case "${_reply:-Y}" in
        [Nn]*) return 1 ;;
        *) return 0 ;;
    esac
}

header "Hephaestus Uninstall"
log "Install location: $PREFIX"
log ""
log "This will remove Hephaestus agents from:"
log "  • pi          ($PI_AGENTS_DIR)"
log "  • Claude Code ($CLAUDE_AGENTS_DIR)"
log "  • CodeGraph   (MCP server config)"
if ! $AGENTS_ONLY; then
log "  • Install dir ($PREFIX)"
fi
log ""

if ! confirm "Proceed with uninstall?"; then
    log "Aborted."
    exit 0
fi

removed=0

# ── Pi agents ──
header "Pi Agents"
if [ -d "$PI_AGENTS_DIR" ]; then
    _pi_count=$(ls -1 "$PI_AGENTS_DIR"/hephaestus-*.md 2>/dev/null | wc -l)
    if [ "$_pi_count" -gt 0 ]; then
        if confirm "Remove $_pi_count Hephaestus agent(s) from pi?"; then
            rm -f "$PI_AGENTS_DIR"/hephaestus-*.md
            ok "Removed $_pi_count pi agent(s)"
            removed=$((removed + _pi_count))
        fi
    else
        log "No Hephaestus pi agents found — skipping"
    fi
else
    log "Pi agents directory not found — skipping"
fi

# ── Claude Code agents ──
header "Claude Code Agents"
if [ -d "$CLAUDE_AGENTS_DIR" ]; then
    _claude_count=$(ls -1 "$CLAUDE_AGENTS_DIR"/hephaestus-*.md 2>/dev/null | wc -l)
    if [ "$_claude_count" -gt 0 ]; then
        if confirm "Remove $_claude_count Hephaestus agent(s) from Claude Code?"; then
            rm -f "$CLAUDE_AGENTS_DIR"/hephaestus-*.md
            ok "Removed $_claude_count Claude Code agent(s)"
            removed=$((removed + _claude_count))
        fi
    else
        log "No Hephaestus Claude Code agents found — skipping"
    fi
else
    log "Claude Code agents directory not found — skipping"
fi

# ── CodeGraph MCP config ──
header "CodeGraph MCP"
if command -v codegraph >/dev/null 2>&1; then
    # Check if codegraph has an uninstall command
    if codegraph uninit --help >/dev/null 2>&1; then
        if confirm "Remove CodeGraph index (.codegraph/) from the current project?"; then
            codegraph uninit . 2>/dev/null && ok "CodeGraph index removed" || warn "No CodeGraph index found"
        fi
    fi

    # Remove MCP server config from Claude Code settings
    if [ -f "$HOME/.claude/settings.json" ] && grep -q 'codegraph' "$HOME/.claude/settings.json" 2>/dev/null; then
        if confirm "Remove CodeGraph MCP server from Claude Code settings?"; then
            # Use python for safe JSON editing if available, otherwise warn
            if command -v python3 >/dev/null 2>&1; then
                python3 -c "
import json, sys
path = '$HOME/.claude/settings.json'
with open(path) as f:
    cfg = json.load(f)
changed = False
for key in ('mcpServers', 'mcp_servers'):
    if key in cfg and 'codegraph' in cfg[key]:
        del cfg[key]['codegraph']
        changed = True
if changed:
    with open(path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print('Removed codegraph from Claude Code settings')
else:
    print('No codegraph entry found in settings')
" 2>/dev/null && ok "Claude Code settings updated" || warn "Could not update Claude Code settings"
            else
                warn "python3 not found — manually remove 'codegraph' from $HOME/.claude/settings.json"
            fi
        fi
    else
        log "CodeGraph not configured in Claude Code settings — skipping"
    fi
else
    log "codegraph CLI not found — skipping CodeGraph cleanup"
fi

# ── Pi MCP config (hephaestus server entry) ──
header "Pi MCP Config"
PI_MCP_CONFIG="$HOME/.config/mcp/mcp.json"
if [ -f "$PI_MCP_CONFIG" ] && grep -q '"heph"' "$PI_MCP_CONFIG" 2>/dev/null; then
    if confirm "Remove hephaestus MCP server from pi config?"; then
        if command -v python3 >/dev/null 2>&1; then
            python3 -c "
import json, sys
path = '$PI_MCP_CONFIG'
with open(path) as f:
    cfg = json.load(f)
changed = False
for key in ('mcpServers', 'mcp_servers', 'servers'):
    if key in cfg and 'heph' in cfg[key]:
        del cfg[key]['heph']
        changed = True
if changed:
    with open(path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print('Removed hephaestus from pi MCP config')
else:
    print('No hephaestus entry found')
" 2>/dev/null && ok "Pi MCP config updated" || warn "Could not update pi MCP config"
        else
            warn "python3 not found — manually remove 'heph' from $PI_MCP_CONFIG"
        fi
    fi
else
    log "No hephaestus MCP entry in pi config — skipping"
fi

# ── Install directory ──
if ! $AGENTS_ONLY; then
    header "Install Directory"
    if [ -d "$PREFIX" ]; then
        log "Hephaestus install at: $PREFIX"
        log ""
        log "This contains:"
        log "  • Source code, venv, config, database"
        log "  • .env file (API keys)"
        log "  • Design docs, workflow history"
        log ""
        if confirm "DELETE the entire install directory ($PREFIX)?"; then
            rm -rf "$PREFIX"
            ok "Removed $PREFIX"
        else
            log "Kept $PREFIX"
        fi
    else
        log "Install directory not found — skipping"
    fi
fi

# ── Summary ──
header "Done"
log "Removed $removed agent file(s)."
if $AGENTS_ONLY; then
    log "Install directory kept: $PREFIX"
else
    log "Run 'heph status' to verify cleanup."
fi
