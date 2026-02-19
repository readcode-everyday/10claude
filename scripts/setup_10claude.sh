#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_10claude.sh — Bootstrap OpenClaw on a fresh machine.
#
# Usage:
#   bash scripts/setup_10claude.sh
#
# What it does:
#   1. Checks prerequisites (Docker, Claude CLI)
#   2. Creates required host directories
#   3. Creates .env from template if missing
#   4. Builds Docker images and starts all containers
#   5. Prints next steps
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Colours ──────────────────────────────────────────────────────────────────
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

if ! command -v docker &>/dev/null; then
    die "Docker not found. Install from https://docs.docker.com/get-docker/"
fi
ok "Docker: $(docker --version | head -1)"

if ! docker compose version &>/dev/null; then
    die "Docker Compose v2 not found. Upgrade Docker Desktop or install the plugin."
fi
ok "Docker Compose: $(docker compose version --short)"

if ! command -v claude &>/dev/null; then
    warn "Claude CLI not found — install it with: npm install -g @anthropic-ai/claude-code"
    warn "Then authenticate: claude auth login"
    warn "The runtime-daemon will fail until Claude CLI is authenticated."
else
    ok "Claude CLI: $(claude --version 2>/dev/null | head -1 || echo 'installed')"
fi

# ── 2. Host directories ───────────────────────────────────────────────────────
echo ""
echo "Creating host directories..."

mkdir -p \
    workspace/queue \
    workspace/results \
    workspace/projects \
    workspace/worktrees \
    workspace/memory \
    workspace/semantic_memory/entries \
    workspace/repo \
    logs \
    backups

ok "Directories ready"

# ── 3. .env file ─────────────────────────────────────────────────────────────
echo ""
if [ ! -f .env ]; then
    warn ".env not found — creating from template"
    cat > .env <<'EOF'
# ── Git identity (for auto-commits inside containers) ─────────────────────────
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

# ── API keys ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-REPLACE_ME
# OPENAI_API_KEY=sk-REPLACE_ME   # optional, for Codex fallback

# ── Web UI basic auth ─────────────────────────────────────────────────────────
# Default password: openclaw
# To change: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'newpass'
# Then replace the hash below — double every $ sign (e.g. $2a → $$2a)
UI_USER=admin
UI_PASS_HASH=$$2a$$14$$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
EOF
    warn ".env created — edit it and set your ANTHROPIC_API_KEY and Git identity before continuing."
    warn "Then re-run this script."
    exit 0
else
    ok ".env exists"

    # Check for placeholder values
    if grep -q "REPLACE_ME" .env; then
        die ".env still has placeholder values. Edit it first (ANTHROPIC_API_KEY etc.)"
    fi
fi

# ── 4. Build + start ──────────────────────────────────────────────────────────
echo ""
echo "Building Docker images..."
docker compose build

echo ""
echo "Starting containers..."
docker compose up -d

echo ""
echo "Waiting for openclaw to be ready..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:18888/api/health >/dev/null 2>&1; then
        ok "openclaw is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        warn "openclaw not responding after 30s — check: docker compose logs openclaw"
    fi
    sleep 1
done

# ── 5. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════"
echo "  OpenClaw is running"
echo ""
echo "  Web UI (local):   http://localhost:18888"
echo "  Web UI (HTTPS):   https://localhost"
echo "  Daemon log:       tail -f $ROOT/logs/daemon.log"
echo ""
echo "  Containers:"
docker compose ps --format "    {{.Name}}  {{.Status}}"
echo ""
echo "  Next steps:"
echo "  1. Open http://localhost:18888 in your browser"
echo "  2. Create a project and submit a task"
echo "  3. For remote access: bash scripts/setup_tailscale.sh"
echo "  4. Change the default UI password (admin/openclaw) — see README"
echo "  ════════════════════════════════════════════"
echo ""
