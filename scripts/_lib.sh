#!/usr/bin/env bash
# _lib.sh — shared functions sourced by setup_10claude.sh and migrate.sh
# Do not run directly.

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
info() { echo -e "${CYAN}[→]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
step() { echo ""; echo -e "${CYAN}── $* ──${NC}"; }

# ── inject_openclaw_config ────────────────────────────────────────────────────
# Merges required gateway keys into ~/.openclaw/openclaw.json without clobbering
# values set by the onboarding wizard (auth token, agent list, Telegram, etc.).
inject_openclaw_config() {
    local cfg="$HOME/.openclaw/openclaw.json"
    [ -f "$cfg" ] || die "~/.openclaw/openclaw.json not found — run 'openclaw configure' first"

    python3 - "$cfg" <<'PYEOF'
import json, sys
path = sys.argv[1]
cfg = json.loads(open(path).read())

# gateway.tools.allow — whitelist sessions_send for HTTP /tools/invoke
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
}

# ── install_gateway ───────────────────────────────────────────────────────────
# Installs (or reinstalls) the openclaw gateway as a systemd user service.
install_gateway() {
    info "Installing openclaw gateway systemd service..."
    openclaw gateway install --port 18789 --force 2>/dev/null \
        || openclaw gateway install --port 18789

    systemctl --user daemon-reload
    systemctl --user enable openclaw-gateway 2>/dev/null || true
    systemctl --user restart openclaw-gateway

    local ready=false
    for i in $(seq 1 20); do
        openclaw gateway health &>/dev/null 2>&1 && ready=true && break
        sleep 1
    done
    $ready \
        && ok "openclaw gateway running on :18789" \
        || die "openclaw gateway failed to start — check: journalctl --user -u openclaw-gateway -n 30"
}

# ── build_images ──────────────────────────────────────────────────────────────
# Builds the status-ui Docker image and the openclaw-sandbox image.
# Expects to be called from the repo root.
build_images() {
    info "Building status-ui image..."
    docker compose build status-ui 2>&1 | grep -E "(DONE|error|ERROR|Successfully)" || true
    ok "status-ui image built"

    info "Building sandbox image..."
    docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox . \
        2>&1 | grep -E "(DONE|error|ERROR|Successfully built)" || true
    ok "openclaw-sandbox:latest built"
}

# ── start_containers ──────────────────────────────────────────────────────────
# Starts (or restarts) all Docker Compose services.
start_containers() {
    info "Starting containers..."
    docker compose up -d --remove-orphans
    ok "Containers started"
}

# ── smoke_tests ───────────────────────────────────────────────────────────────
# Waits for status-ui to respond and checks the gateway.
smoke_tests() {
    local ui_ok=false
    for i in $(seq 1 30); do
        curl -sf http://localhost:18888/api/health >/dev/null 2>&1 && ui_ok=true && break
        sleep 1
    done
    $ui_ok \
        && ok "status-ui: $(curl -s http://localhost:18888/api/health)" \
        || warn "status-ui not responding after 30 s — check: docker compose logs status-ui"

    openclaw gateway health &>/dev/null \
        && ok "openclaw gateway: OK" \
        || warn "openclaw gateway: not responding"

    echo ""
    docker compose ps --format "  {{.Name}}  {{.Status}}"
}
