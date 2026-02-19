#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# migrate.sh — Move the full OpenClaw stack to a new machine.
#
# Run this ON THE NEW MACHINE after copying state from the old one:
#
#   # On old machine — copy state (adjust user@new-host):
#   rsync -av ~/10claude/          user@new-host:~/10claude/
#   rsync -av ~/.openclaw/         user@new-host:~/.openclaw/
#   rsync -av ~/.ssh/              user@new-host:~/.ssh/
#   # Copy .env separately (not in git):
#   scp ~/10claude/.env            user@new-host:~/10claude/.env
#
#   # On new machine:
#   bash ~/10claude/scripts/migrate.sh
#
# What this script does:
#   1. Install system dependencies (Docker, Node.js, npm)
#   2. Install Claude CLI and openclaw npm packages
#   3. Validate .env and workspace state
#   4. Install openclaw gateway as a systemd user service
#   5. Build Docker images (status-ui, sandbox)
#   6. Start Docker containers
#   7. Smoke-test all components
#   8. Remind about the one step that can't be automated (claude auth login)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
info() { echo -e "${CYAN}[→]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
step() { echo ""; echo -e "${CYAN}── $* ──${NC}"; }

echo ""
echo "  OpenClaw migration"
echo "  ══════════════════"
echo "  Target host: $(hostname)"
echo "  Date:        $(date '+%Y-%m-%d %H:%M')"
echo ""

# ── 1. System dependencies ────────────────────────────────────────────────────
step "System dependencies"

# Docker
if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version | head -1)"
else
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    warn "Added $USER to docker group — you may need to log out and back in"
    warn "If docker commands fail below, run: newgrp docker"
fi

if ! docker compose version &>/dev/null; then
    die "Docker Compose v2 not found. Upgrade Docker or install the compose plugin."
fi
ok "Docker Compose: $(docker compose version --short)"

# Node.js — check version >= 18
if command -v node &>/dev/null; then
    NODE_VER=$(node --version | sed 's/v//' | cut -d. -f1)
    if [ "$NODE_VER" -ge 18 ]; then
        ok "Node.js: $(node --version)"
    else
        warn "Node.js $(node --version) is too old (need >=18) — installing via NodeSource"
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
else
    info "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
    ok "Node.js: $(node --version)"
fi

# npm global prefix for non-root installs
NPM_PREFIX="$HOME/.npm-global"
mkdir -p "$NPM_PREFIX"
npm config set prefix "$NPM_PREFIX" 2>/dev/null || true

# Ensure ~/.npm-global/bin is on PATH now and in future shells
export PATH="$NPM_PREFIX/bin:$PATH"
if ! grep -q "npm-global/bin" "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$HOME/.bashrc"
    info "Added ~/.npm-global/bin to PATH in ~/.bashrc"
fi

# Claude CLI
if command -v claude &>/dev/null; then
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
else
    info "Installing Claude CLI..."
    npm install -g @anthropic-ai/claude-code
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
fi

# openclaw
if command -v openclaw &>/dev/null; then
    ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"
else
    info "Installing openclaw..."
    npm install -g openclaw
    ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"
fi

# ── 2. Validate copied state ──────────────────────────────────────────────────
step "Validating copied state"

# ~/.openclaw/
if [ -f "$HOME/.openclaw/openclaw.json" ]; then
    ok "~/.openclaw/openclaw.json present"
else
    die "~/.openclaw/openclaw.json not found. Did you rsync ~/.openclaw/ from the old machine?"
fi

# .env
if [ -f "$ROOT/.env" ]; then
    ok ".env present"
    if grep -q "REPLACE_ME" "$ROOT/.env"; then
        die ".env still has REPLACE_ME placeholders — edit it first"
    fi
else
    die ".env not found. Copy it from the old machine: scp old-host:~/10claude/.env ~/10claude/.env"
fi

# workspace
if [ -d "$ROOT/workspace/projects" ]; then
    PROJECT_COUNT=$(find "$ROOT/workspace/projects" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
    ok "workspace/projects present ($PROJECT_COUNT project(s))"
else
    warn "workspace/projects not found — creating empty workspace structure"
fi

# Create any missing workspace dirs
mkdir -p \
    "$ROOT/workspace/queue" \
    "$ROOT/workspace/results" \
    "$ROOT/workspace/projects" \
    "$ROOT/workspace/worktrees" \
    "$ROOT/workspace/memory" \
    "$ROOT/logs" \
    "$ROOT/backups"
ok "Workspace directories ready"

# SSH keys
if [ -d "$HOME/.ssh" ] && ls "$HOME/.ssh"/id_* &>/dev/null 2>&1; then
    ok "SSH keys present"
    chmod 700 "$HOME/.ssh"
    chmod 600 "$HOME/.ssh"/id_* 2>/dev/null || true
else
    warn "No SSH keys found in ~/.ssh/ — git push to private remotes may fail"
    warn "Either copy keys from old machine or generate new ones: ssh-keygen -t ed25519"
fi

# ── 3. openclaw gateway (systemd) ─────────────────────────────────────────────
step "openclaw gateway service"

# Re-issue the gateway install (it writes a fresh systemd unit with current paths)
info "Installing openclaw gateway systemd service..."
openclaw gateway install --port 18789 --force 2>/dev/null || openclaw gateway install --port 18789

systemctl --user daemon-reload
systemctl --user enable openclaw-gateway 2>/dev/null || true
systemctl --user restart openclaw-gateway

# Wait for gateway to be healthy
GATEWAY_READY=false
for i in $(seq 1 20); do
    if openclaw gateway health &>/dev/null 2>&1; then
        GATEWAY_READY=true
        break
    fi
    sleep 1
done

if $GATEWAY_READY; then
    ok "openclaw gateway running on port 18789"
else
    die "openclaw gateway failed to start — check: journalctl --user -u openclaw-gateway -n 30"
fi

# ── 4. Docker images ──────────────────────────────────────────────────────────
step "Docker images"

info "Building status-ui image..."
docker compose build --no-cache status-ui 2>&1 | grep -E "(Step|CACHED|error|ERROR|Successfully)" || true
ok "status-ui image built"

info "Building sandbox image..."
docker build -t openclaw-sandbox:latest -f "$ROOT/Dockerfile.sandbox" "$ROOT" \
    2>&1 | grep -E "(Step|CACHED|DONE|error|ERROR|Successfully built)" || true
ok "openclaw-sandbox:latest built"

# ── 5. Docker containers ──────────────────────────────────────────────────────
step "Docker containers"

# Stop any leftover containers from old machine names
docker stop openclaw runtime-daemon 2>/dev/null && docker rm openclaw runtime-daemon 2>/dev/null || true

info "Starting containers..."
docker compose up -d --remove-orphans
ok "Containers started"

# ── 6. Smoke tests ────────────────────────────────────────────────────────────
step "Smoke tests"

# status-ui health
STATUS_UI_OK=false
for i in $(seq 1 30); do
    if curl -sf http://localhost:18888/api/health >/dev/null 2>&1; then
        STATUS_UI_OK=true
        break
    fi
    sleep 1
done

if $STATUS_UI_OK; then
    HEALTH=$(curl -s http://localhost:18888/api/health)
    ok "status-ui: $HEALTH"
else
    warn "status-ui not responding after 30s — check: docker compose logs status-ui"
fi

# openclaw gateway
openclaw gateway health &>/dev/null && ok "openclaw gateway: OK" || warn "openclaw gateway: not responding"

# Container list
echo ""
docker compose ps --format "  {{.Name}}  {{.Status}}  {{.Ports}}"

# ── 7. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════════════════════"
echo "  Migration complete on $(hostname)"
echo ""
echo "  Services:"
echo "    openclaw gateway  →  http://localhost:18789  (systemd)"
echo "    status-ui         →  http://localhost:18888  (docker)"
echo "    caddy (TLS+auth)  →  https://localhost       (docker)"
echo ""
echo -e "  ${RED}REQUIRED MANUAL STEP — cannot be automated:${NC}"
echo ""
echo "    claude auth login"
echo ""
echo "  Claude CLI auth is device-bound. You must re-authenticate"
echo "  interactively on this machine before any coding tasks will run."
echo ""
echo "  After auth, submit a small test task at http://localhost:18888"
echo "  to verify sandbox containers spin up correctly."
echo ""
if [ -d "$HOME/.ssh" ] && ! ls "$HOME/.ssh"/id_* &>/dev/null 2>&1; then
    echo -e "  ${YELLOW}ALSO NEEDED:${NC} SSH keys (git push to private repos will fail)"
    echo "    ssh-keygen -t ed25519 -C your@email.com"
    echo "    # Then add the public key to GitHub/GitLab"
    echo ""
fi
echo "  ════════════════════════════════════════════════════════════"
echo ""
