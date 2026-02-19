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
#   1. Install system dependencies (Docker, Node.js, npm) if missing
#   2. Install Claude CLI and openclaw npm packages
#   3. Validate copied state (.env, openclaw.json, workspace dirs, SSH keys)
#   4. Inject required openclaw.json config keys
#   5. Install openclaw gateway as a systemd user service
#   6. Build Docker images (status-ui, sandbox)
#   7. Start Docker containers (clean up old container names first)
#   8. Smoke-test all components
#   9. Remind about the one step that can't be automated (claude auth login)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/_lib.sh
source "$ROOT/scripts/_lib.sh"

echo ""
echo "  OpenClaw migration"
echo "  ══════════════════"
echo "  Target host: $(hostname)"
echo "  Date:        $(date '+%Y-%m-%d %H:%M')"
echo ""

# ── 1. System dependencies ────────────────────────────────────────────────────
step "System dependencies"

if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version | head -1)"
else
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    warn "Added $USER to docker group — you may need to log out and back in"
    warn "If docker commands fail below, run: newgrp docker"
fi

docker compose version &>/dev/null \
    || die "Docker Compose v2 not found. Upgrade Docker or install the compose plugin."
ok "Docker Compose: $(docker compose version --short)"

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
export PATH="$NPM_PREFIX/bin:$PATH"
if ! grep -q "npm-global/bin" "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$HOME/.bashrc"
    info "Added ~/.npm-global/bin to PATH in ~/.bashrc"
fi

# ── 2. npm packages ───────────────────────────────────────────────────────────
step "npm packages"

if command -v claude &>/dev/null; then
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
else
    info "Installing Claude CLI..."
    npm install -g @anthropic-ai/claude-code
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
fi

if command -v openclaw &>/dev/null; then
    ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"
else
    info "Installing openclaw..."
    npm install -g openclaw
    ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"
fi

# ── 3. Validate copied state ──────────────────────────────────────────────────
step "Validating copied state"

[ -f "$HOME/.openclaw/openclaw.json" ] \
    || die "~/.openclaw/openclaw.json not found. Did you rsync ~/.openclaw/ from the old machine?"
ok "~/.openclaw/openclaw.json present"

[ -f "$ROOT/.env" ] \
    || die ".env not found. Copy it: scp old-host:~/10claude/.env ~/10claude/.env"
ok ".env present"
grep -q "REPLACE_ME" "$ROOT/.env" \
    && die ".env still has REPLACE_ME placeholders — edit it first" || true

if [ -d "$ROOT/workspace/projects" ]; then
    PROJECT_COUNT=$(find "$ROOT/workspace/projects" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
    ok "workspace/projects present ($PROJECT_COUNT project(s))"
else
    warn "workspace/projects not found — creating empty workspace structure"
fi

mkdir -p \
    "$ROOT/workspace/queue" \
    "$ROOT/workspace/results" \
    "$ROOT/workspace/projects" \
    "$ROOT/workspace/worktrees" \
    "$ROOT/workspace/memory" \
    "$ROOT/logs" \
    "$ROOT/backups" \
    "$HOME/.openclaw/workspace/personal/memory" \
    "$HOME/.openclaw/workspace/dev/memory"
ok "Workspace directories ready"

if [ -d "$HOME/.ssh" ] && ls "$HOME/.ssh"/id_* &>/dev/null 2>&1; then
    ok "SSH keys present"
    chmod 700 "$HOME/.ssh"
    chmod 600 "$HOME/.ssh"/id_* 2>/dev/null || true
else
    warn "No SSH keys in ~/.ssh/ — git push to private remotes may fail"
    warn "Copy from old machine or generate: ssh-keygen -t ed25519"
fi

# ── 4. openclaw.json ──────────────────────────────────────────────────────────
step "openclaw.json required config"
inject_openclaw_config

# ── 5. openclaw gateway (systemd) ─────────────────────────────────────────────
step "openclaw gateway"
install_gateway

# ── 6. Docker images ──────────────────────────────────────────────────────────
step "Docker images"
build_images

# ── 7. Docker containers ──────────────────────────────────────────────────────
step "Containers"

# Clean up leftover containers from old machine names (|| true so set -e doesn't abort)
docker stop openclaw runtime-daemon 2>/dev/null; docker rm openclaw runtime-daemon 2>/dev/null; true

start_containers

# ── 8. Smoke tests ────────────────────────────────────────────────────────────
step "Smoke tests"
smoke_tests

# ── 9. Summary ────────────────────────────────────────────────────────────────
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
