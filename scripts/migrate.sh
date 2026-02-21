#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# migrate.sh — Bootstrap or migrate the OpenClaw stack.
#
# Works for both fresh installs and moving to a new machine.
#
# Fresh install:
#   git clone <repo> ~/10claude && cd ~/10claude
#   bash scripts/migrate.sh
#
# Migration from another machine — copy state first:
#   # On old machine:
#   rsync -av ~/10claude/          user@new-host:~/10claude/
#   rsync -av ~/.openclaw/         user@new-host:~/.openclaw/
#   rsync -av ~/.ssh/              user@new-host:~/.ssh/
#   scp ~/10claude/.env            user@new-host:~/10claude/.env
#   # On new machine:
#   bash ~/10claude/scripts/migrate.sh
#
# What this script does:
#   1. Install system dependencies (Docker, Node.js, npm) if missing
#   2. Install Claude CLI and openclaw npm packages if missing
#   3. Create or validate .env
#   4. Create workspace directories
#   5. Inject required openclaw.json config keys
#   6. Install openclaw gateway as a systemd user service
#   7. Build Docker images (status-ui, sandbox)
#   8. Start Docker containers
#   9. Smoke-test all components
#  10. Remind about the one step that can't be automated (claude auth login)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
info() { echo -e "${CYAN}[→]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
step() { echo ""; echo -e "${CYAN}── $* ──${NC}"; }

echo ""
echo "  OpenClaw setup"
echo "  ══════════════"
echo "  Host: $(hostname)"
echo "  Date: $(date '+%Y-%m-%d %H:%M')"
echo ""

# ── 1. System dependencies ────────────────────────────────────────────────────
step "System dependencies"

if command -v docker &>/dev/null; then
    ok "Docker: $(docker --version | head -1)"
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
        warn "Node.js $(node --version) is too old (need >=18) — upgrading via NodeSource"
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
        ok "Node.js: $(node --version)"
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
    ok "Claude CLI installed"
fi

if command -v openclaw &>/dev/null; then
    ok "openclaw: $(openclaw --version 2>/dev/null || echo 'installed')"
else
    info "Installing openclaw..."
    npm install -g openclaw
    ok "openclaw installed"
fi

if command -v gh &>/dev/null; then
    ok "GitHub CLI: $(gh --version | head -1)"
else
    info "Installing GitHub CLI (gh)..."
    type -p curl &>/dev/null || sudo apt-get install -y curl
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    sudo apt-get update -q && sudo apt-get install -y gh
    ok "GitHub CLI installed"
fi

# ── 3. .env ───────────────────────────────────────────────────────────────────
step ".env"

if [ ! -f "$ROOT/.env" ]; then
    warn ".env not found — creating from template"
    HOST_UID_VAL=$(id -u)
    HOST_GID_VAL=$(id -g)
    GIT_USER=$(git config --global user.name 2>/dev/null || echo "your-name")
    GIT_EMAIL=$(git config --global user.email 2>/dev/null || echo "your@email.com")
    GH_USER=$(gh api user --jq .login 2>/dev/null || echo "your-github-username")
    GH_TOKEN=$(gh auth token 2>/dev/null || echo "ghp_REPLACE_ME")
    cat > "$ROOT/.env" <<EOF
# Host user IDs — used to chown project repos created inside containers
HOST_UID=$HOST_UID_VAL
HOST_GID=$HOST_GID_VAL

# Git identity (for auto-commits inside sandbox containers)
GIT_AUTHOR_NAME=$GIT_USER
GIT_AUTHOR_EMAIL=$GIT_EMAIL
GIT_COMMITTER_NAME=$GIT_USER
GIT_COMMITTER_EMAIL=$GIT_EMAIL

# API key for plan generation (must be sk-ant-api03-..., not an OAuth token)
ANTHROPIC_API_KEY=sk-ant-REPLACE_ME

# GitHub integration — create private repos and push commits automatically
# Requires 'repo' scope. Get with: gh auth token  (after: gh auth login)
GITHUB_TOKEN=$GH_TOKEN
GITHUB_USER=$GH_USER

# Web UI basic auth (default password: openclaw)
# To change: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'newpass'
# Double every dollar sign in the hash (e.g. \$2a → \$\$2a)
UI_USER=admin
UI_PASS_HASH=\$\$2a\$\$14\$\$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
EOF
    warn ".env created — edit ANTHROPIC_API_KEY if needed, then re-run."
    exit 0
fi

ok ".env present"
grep -q "REPLACE_ME" "$ROOT/.env" && die ".env still has placeholder values — edit it first" || true

# ── 4. Workspace directories ──────────────────────────────────────────────────
step "Workspace"

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

# ── 5. openclaw.json — inject required config keys ────────────────────────────
step "openclaw.json"

[ -f "$HOME/.openclaw/openclaw.json" ] \
    || die "~/.openclaw/openclaw.json not found — run 'openclaw configure' first, then re-run this script"

# Merge required keys without clobbering wizard-set values (auth token, agents, Telegram…)
python3 - "$HOME/.openclaw/openclaw.json" <<'PYEOF'
import json, sys
path = sys.argv[1]
cfg = json.loads(open(path).read())

# gateway.tools.allow — whitelist sessions_send/spawn for HTTP /tools/invoke
gw = cfg.setdefault("gateway", {})
allow = gw.setdefault("tools", {}).setdefault("allow", [])
for tool in ["sessions_send", "sessions_spawn"]:
    if tool not in allow:
        allow.append(tool)

# top-level tools — cross-agent visibility and routing
tools = cfg.setdefault("tools", {})
tools.setdefault("sessions", {})["visibility"] = "all"
tools.setdefault("agentToAgent", {})["enabled"] = True

open(path, "w").write(json.dumps(cfg, indent=2))
print("  gateway.tools.allow, tools.sessions.visibility, tools.agentToAgent — OK")
PYEOF
ok "openclaw.json required keys present"

# ── 6. openclaw gateway (systemd) ─────────────────────────────────────────────
step "openclaw gateway"

info "Installing openclaw gateway systemd service..."
openclaw gateway install --port 18789 --force 2>/dev/null \
    || openclaw gateway install --port 18789

# Patch ExecStart to run under the docker group so the gateway can manage
# sandbox containers. systemd user services cannot use SupplementaryGroups,
# so we wrap the node command with `sg docker`.
SERVICE_FILE="$HOME/.config/systemd/user/openclaw-gateway.service"
if [ -f "$SERVICE_FILE" ] && ! grep -q "sg docker" "$SERVICE_FILE"; then
    info "Patching gateway service to run with docker group..."
    python3 - "$SERVICE_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
content = open(path).read()
def patch(m):
    orig = m.group(1)
    return f"ExecStart=/usr/bin/sg docker -c '{orig}'"
content = re.sub(r'^ExecStart=(.+)$', patch, content, flags=re.MULTILINE)
open(path, 'w').write(content)
print("  ExecStart wrapped with sg docker")
PYEOF
    ok "Gateway service patched with sg docker wrapper"
fi

systemctl --user daemon-reload
systemctl --user enable openclaw-gateway 2>/dev/null || true
systemctl --user restart openclaw-gateway

# ── 6b. openclaw watchdog (git backup, Claude ping, health checks) ────────────
step "openclaw watchdog"

WATCHDOG_SERVICE="$HOME/.config/systemd/user/openclaw-watchdog.service"
cat > "$WATCHDOG_SERVICE" <<SVCEOF
[Unit]
Description=OpenClaw Watchdog — git backup, Claude ping, health checks, Telegram alerts
After=network-online.target openclaw-gateway.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $ROOT/scripts/watchdog.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=PATH=$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$ROOT

[Install]
WantedBy=default.target
SVCEOF

systemctl --user daemon-reload
systemctl --user enable openclaw-watchdog 2>/dev/null || true
systemctl --user restart openclaw-watchdog
sleep 2
systemctl --user is-active openclaw-watchdog &>/dev/null \
    && ok "openclaw watchdog running" \
    || warn "openclaw watchdog failed to start — check: journalctl --user -u openclaw-watchdog -n 20"

GATEWAY_READY=false
for i in $(seq 1 20); do
    openclaw gateway health &>/dev/null 2>&1 && GATEWAY_READY=true && break
    sleep 1
done
$GATEWAY_READY \
    && ok "openclaw gateway running on :18789" \
    || die "openclaw gateway failed to start — check: journalctl --user -u openclaw-gateway -n 30"

# ── 7. Docker images ──────────────────────────────────────────────────────────
step "Docker images"

info "Building status-ui image..."
docker compose build status-ui 2>&1 | grep -E "(DONE|error|ERROR|Successfully)" || true
ok "status-ui image built"

info "Building sandbox image..."
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox . \
    2>&1 | grep -E "(DONE|error|ERROR|Successfully built)" || true
ok "openclaw-sandbox:latest built"

# ── 8. Containers ─────────────────────────────────────────────────────────────
step "Containers"

# Remove leftover containers from old machine / old service names
docker stop openclaw runtime-daemon 2>/dev/null; docker rm openclaw runtime-daemon 2>/dev/null; true

info "Starting containers..."
docker compose up -d --remove-orphans
ok "Containers started"

# ── 9. Smoke tests ────────────────────────────────────────────────────────────
step "Smoke tests"

UI_OK=false
for i in $(seq 1 30); do
    curl -sf http://localhost:18888/api/health >/dev/null 2>&1 && UI_OK=true && break
    sleep 1
done
$UI_OK \
    && ok "status-ui: $(curl -s http://localhost:18888/api/health)" \
    || warn "status-ui not responding after 30 s — check: docker compose logs status-ui"

openclaw gateway health &>/dev/null \
    && ok "openclaw gateway: OK" \
    || warn "openclaw gateway: not responding"

echo ""
docker compose ps --format "  {{.Name}}  {{.Status}}"

# ── 10. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════════════════════"
echo "  OpenClaw is running on $(hostname)"
echo ""
echo "  Services:"
echo "    openclaw gateway  →  http://localhost:18789  (systemd)"
echo "    openclaw watchdog →  background threads      (systemd)"
echo "    status-ui         →  http://localhost:18888  (docker)"
echo "    caddy (TLS+auth)  →  https://localhost       (docker)"
echo ""
echo "  Watchdog (scripts/watchdog.py):"
echo "    • Hourly git backup of ~/10claude and ~/.openclaw/workspace"
echo "    • Hourly Claude session ping (keeps OAuth token warm)"
echo "    • Health check every 5 min: containers, gateway, status-ui, stuck tasks"
echo "    • Proactive Telegram alerts on new issues and resolutions"
echo "    • Self-healing: dead threads auto-restarted; systemd restarts process on crash"
echo ""
echo -e "  ${RED}REQUIRED — cannot be automated:${NC}"
echo ""
echo "    claude auth login           # Claude CLI device auth (always required)"
echo "    gh auth login               # GitHub CLI — needed for repo creation + push"
echo ""
echo "  Claude CLI auth is device-bound. Re-authenticate on every new machine."
echo "  Credentials are stored in ~/.claude/.credentials.json and are bind-mounted"
echo "  into each worker container — workers authenticate using the same session."
echo ""
echo "  After gh auth login, update .env with your token:"
echo "    GITHUB_TOKEN=\$(gh auth token)"
echo "    GITHUB_USER=\$(gh api user --jq .login)"
echo "  Then: docker compose up -d --force-recreate status-ui worker-1 worker-2 worker-3"
echo ""
echo "  After auth, submit a test task at http://localhost:18888 to verify"
echo "  all 3 workers pick up tasks in parallel."
echo ""
echo -e "  ${CYAN}OPTIONAL — Restore openclaw workspace (agent rules, identity):${NC}"
echo ""
echo "  The agent workspace (AGENTS.md, SOUL.md, etc.) is backed up at:"
echo "    github.com/dark-knight-0007/openclaw-workspace  (private repo)"
echo "  To restore on a new machine:"
echo "    git clone https://\$(gh auth token)@github.com/dark-knight-0007/openclaw-workspace.git \\"
echo "        ~/.openclaw/workspace-restored"
echo "    # Then merge or copy the files into ~/.openclaw/workspace/"
echo "    # (openclaw configure creates this dir first)"
echo ""
echo -e "  ${CYAN}OPTIONAL — Telegram channel (recommended):${NC}"
echo ""
echo "  openclaw has built-in Telegram support — no custom bot code needed."
echo "  1. Create a bot: message @BotFather on Telegram → /newbot → copy token"
echo "  2. Connect:"
echo "       openclaw channels add --channel telegram --token <your-bot-token>"
echo "  3. Open your bot in Telegram and send /start to complete pairing"
echo "  4. Send tasks via Telegram: 'Queue a task: <description> in <project>'"
echo "     The dev agent forwards requests to the worker queue automatically."
echo ""
echo "  Other supported channels: WhatsApp, Discord, Slack, Signal, Matrix, iMessage"
echo "     openclaw channels add --channel discord  --token <token>"
echo "     openclaw channels add --channel whatsapp  # QR code flow"
echo ""
if [ -d "$HOME/.ssh" ] && ! ls "$HOME/.ssh"/id_* &>/dev/null 2>&1; then
    echo -e "  ${YELLOW}ALSO NEEDED:${NC} SSH keys (git push to private repos will fail)"
    echo "    ssh-keygen -t ed25519 -C your@email.com"
    echo "    # Then add the public key to GitHub/GitLab"
    echo ""
fi
echo "  ════════════════════════════════════════════════════════════"
echo ""
