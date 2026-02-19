#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_10claude.sh — Bootstrap OpenClaw on a fresh machine.
#
# Usage:
#   bash scripts/setup_10claude.sh
#
# What it does:
#   1. Checks prerequisites (Docker, Node.js, Claude CLI, openclaw)
#   2. Creates required workspace directories
#   3. Creates .env from template if missing
#   4. Installs openclaw gateway as a systemd user service
#   5. Builds Docker images and starts containers
#   6. Smoke-tests all components
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

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
    || die "Node.js not found. Install via: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs"
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
    logs \
    backups
ok "Directories ready"

# ── 3. .env file ─────────────────────────────────────────────────────────────
echo ""
if [ ! -f .env ]; then
    warn ".env not found — creating from template"
    cat > .env <<'EOF'
# ── Git identity (for auto-commits inside sandbox containers) ─────────────────
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

# ── API key for plan generation ───────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-REPLACE_ME

# ── Web UI basic auth ─────────────────────────────────────────────────────────
# Default password: openclaw
# To change: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'newpass'
# Then replace the hash below — double every $ sign (e.g. $2a → $$2a)
UI_USER=admin
UI_PASS_HASH=$$2a$$14$$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
EOF
    warn ".env created — edit it and set ANTHROPIC_API_KEY and Git identity, then re-run."
    exit 0
else
    ok ".env exists"
    grep -q "REPLACE_ME" .env && die ".env still has placeholder values — edit it first"
fi

# ── 4. openclaw gateway (systemd) ────────────────────────────────────────────
echo ""
echo "Installing openclaw gateway service..."

openclaw gateway install --port 18789 --force 2>/dev/null || openclaw gateway install --port 18789

systemctl --user daemon-reload
systemctl --user enable openclaw-gateway 2>/dev/null || true
systemctl --user restart openclaw-gateway

for i in $(seq 1 15); do
    openclaw gateway health &>/dev/null && break
    sleep 1
done
openclaw gateway health &>/dev/null \
    && ok "openclaw gateway running on :18789" \
    || warn "openclaw gateway may not be ready — check: journalctl --user -u openclaw-gateway -n 20"

# ── 5. Docker images + containers ────────────────────────────────────────────
echo ""
echo "Building Docker images..."
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox . \
    2>&1 | grep -E "(DONE|error|ERROR|Successfully)" || true
ok "openclaw-sandbox:latest built"

docker compose build
echo ""
echo "Starting containers..."
docker compose up -d --remove-orphans

# ── 6. Smoke test ─────────────────────────────────────────────────────────────
echo ""
echo "Waiting for status-ui..."
for i in $(seq 1 30); do
    curl -sf http://localhost:18888/api/health >/dev/null 2>&1 && break
    [ "$i" -eq 30 ] && warn "status-ui not responding — check: docker compose logs status-ui"
    sleep 1
done
curl -sf http://localhost:18888/api/health >/dev/null 2>&1 \
    && ok "status-ui ready: $(curl -s http://localhost:18888/api/health)" \
    || true

# ── 7. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════════════"
echo "  OpenClaw is running on $(hostname)"
echo ""
echo "  Services:"
echo "    openclaw gateway  →  :18789  (systemd)"
echo "    status-ui         →  http://localhost:18888"
echo "    caddy (TLS+auth)  →  https://localhost"
echo ""
echo "  Containers:"
docker compose ps --format "    {{.Name}}  {{.Status}}"
echo ""
echo "  Next steps:"
echo "  1. Open http://localhost:18888"
echo "  2. Authenticate Claude CLI if not done: claude auth login"
echo "  3. For remote access: bash scripts/setup_tailscale.sh"
echo "  ════════════════════════════════════════════════════"
echo ""
