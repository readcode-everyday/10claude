#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_10claude.sh — Bootstrap OpenClaw on a machine where tools are already
# installed (Docker, Node.js, openclaw, Claude CLI).
#
# Usage:
#   bash scripts/setup_10claude.sh
#
# What it does:
#   1. Checks prerequisites (Docker, Node.js, Claude CLI, openclaw)
#   2. Creates required workspace directories
#   3. Creates .env from template if missing
#   4. Injects required openclaw.json config keys
#   5. Installs openclaw gateway as a systemd user service
#   6. Builds Docker images and starts containers
#   7. Smoke-tests all components
#
# For a full migration from another machine (including installing prerequisites),
# use scripts/migrate.sh instead.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/_lib.sh
source "$ROOT/scripts/_lib.sh"

echo ""
echo "  OpenClaw bootstrap"
echo "  ══════════════════"
echo ""

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
echo "Checking prerequisites..."

command -v docker &>/dev/null \
    || die "Docker not found. Install from https://docs.docker.com/get-docker/"
ok "Docker: $(docker --version | head -1)"

docker compose version &>/dev/null \
    || die "Docker Compose v2 not found. Upgrade Docker Desktop or install the plugin."
ok "Docker Compose: $(docker compose version --short)"

command -v node &>/dev/null \
    || die "Node.js not found. Install via NodeSource: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs"
ok "Node.js: $(node --version)"

command -v openclaw &>/dev/null \
    || die "openclaw not found. Install: npm install -g openclaw"
ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"

if command -v claude &>/dev/null; then
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
else
    warn "Claude CLI not found — install: npm install -g @anthropic-ai/claude-code"
    warn "Then authenticate: claude auth login"
    warn "Sandbox workers will fail until Claude CLI is authenticated."
fi

# ── 2. Workspace directories ──────────────────────────────────────────────────
echo ""
echo "Creating workspace directories..."
mkdir -p \
    workspace/queue \
    workspace/results \
    workspace/projects \
    workspace/worktrees \
    workspace/memory \
    logs \
    backups
ok "Directories ready"

# ── 3. .env file ─────────────────────────────────────────────────────────────
echo ""
if [ ! -f .env ]; then
    warn ".env not found — creating from template"
    HOST_UID=$(id -u)
    HOST_GID=$(id -g)
    cat > .env <<EOF
# ── Host user IDs — used to chown project repos created inside containers ─────
HOST_UID=$HOST_UID
HOST_GID=$HOST_GID

# ── Git identity (for auto-commits inside sandbox containers) ─────────────────
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

# ── API key for plan generation ───────────────────────────────────────────────
# Must be a standard API key (sk-ant-api03-...), not an OAuth token
ANTHROPIC_API_KEY=sk-ant-REPLACE_ME

# ── Web UI basic auth ─────────────────────────────────────────────────────────
# Default password: openclaw
# To change: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'newpass'
# Then replace the hash below — double every \$ sign (e.g. \$2a → \$\$2a)
UI_USER=admin
UI_PASS_HASH=\$\$2a\$\$14\$\$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
EOF
    warn ".env created — edit it and set ANTHROPIC_API_KEY and Git identity, then re-run."
    exit 0
else
    ok ".env exists"
    grep -q "REPLACE_ME" .env && die ".env still has placeholder values — edit it first"
fi

# ── 4. openclaw.json ──────────────────────────────────────────────────────────
echo ""
step "openclaw.json required config"
inject_openclaw_config

# ── 5. openclaw gateway (systemd) ────────────────────────────────────────────
echo ""
step "openclaw gateway"
install_gateway

# ── 6. Docker images + containers ────────────────────────────────────────────
echo ""
step "Docker images"
build_images

echo ""
step "Containers"
start_containers

# ── 7. Smoke test ─────────────────────────────────────────────────────────────
echo ""
step "Smoke tests"
smoke_tests

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════════════"
echo "  OpenClaw is running on $(hostname)"
echo ""
echo "  Services:"
echo "    openclaw gateway  →  :18789  (systemd)"
echo "    status-ui         →  http://localhost:18888"
echo "    caddy (TLS+auth)  →  https://localhost"
echo ""
echo "  Next steps:"
echo "  1. Open http://localhost:18888"
echo "  2. Authenticate Claude CLI if not done: claude auth login"
echo "  3. For remote access: bash scripts/setup_tailscale.sh"
echo "  ════════════════════════════════════════════════════"
echo ""
