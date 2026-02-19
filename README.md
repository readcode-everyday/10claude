# OpenClaw — Autonomous Dev Manager

OpenClaw is a self-hosted autonomous software engineering system.
You describe what you want done; the system plans, executes, and evaluates the work using Claude as its coding engine — without human intervention.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Components](#components)
3. [Data Formats](#data-formats)
4. [Directory Structure](#directory-structure)
5. [Prerequisites](#prerequisites)
6. [Fresh Install](#fresh-install)
7. [Configuration](#configuration)
8. [Web UI & Remote Access](#web-ui--remote-access)
9. [How to Use](#how-to-use)
10. [Migration to a New Machine](#migration-to-a-new-machine)
11. [Recovery & Restore](#recovery--restore)
12. [Troubleshooting](#troubleshooting)

---

## Architecture

```
You (browser)
      │
      │  HTTPS :443
      ▼
   [ Caddy ]  — TLS termination + basic auth  (Docker, permanent)
      │
      │  HTTP :18888
      ▼
   [ status-ui ]  — FastAPI + Kanban UI  (Docker, permanent)
      │  POST /tools/invoke  →  sessions_send  →  agent:dev:main
      │  (synchronous: blocks per-task background thread up to 10 min for reply)
      │
      │  HTTP :18789
      ▼
   [ openclaw gateway ]  — AI messaging gateway  (native systemd, permanent)
      │
      │  agent:dev:main  receives task message, spawns sub-agents as needed
      ▼
   [ openclaw-sandbox ]  — ephemeral Docker containers  (one per coding worker)
      │  runs: claude --dangerously-skip-permissions -p '<task>'
      │  mounts: workspace/projects, workspace/worktrees, ~/.claude
      │
      ▼
   workspace/results/{task_id}.json
```

**Key design principle:** all state lives on the host (`~/10claude/` and `~/.openclaw/`).
Containers are disposable — nothing is lost on restart or rebuild.

---

## Components

### 1. openclaw gateway
**What it is:** The [openclaw](https://openclaw.ai) npm package — an AI messaging and agent orchestration gateway. This is the execution engine that replaced the old custom Python `runtime-daemon`.

**Location:** Native host process, not in Docker.
**Binary:** `~/.npm-global/bin/openclaw`
**Config:** `~/.openclaw/openclaw.json`
**Managed by:** systemd user service `openclaw-gateway`
**Port:** 18789 (WebSocket + HTTP multiplex)

**What it provides natively (no custom code needed):**
- **Agent sessions** — isolated conversation contexts per task, each with its own system prompt, memory, and tool access
- **Sub-agent spawning** (`sessions_spawn` tool) — spawns parallel background workers, up to `maxConcurrent` at once; workers announce results back when done
- **Sandbox containers** — runs tool execution inside Docker containers to limit filesystem blast radius when using `--dangerously-skip-permissions`
- **Memory system** — hybrid BM25 + vector search over plain Markdown daily logs, SQLite index per agent, auto-maintained `MEMORY.md` summary
- **Workspace files** — each agent reads `AGENTS.md` (rules), `SOUL.md` (identity), `MEMORY.md` (summary) from its workspace dir on every session
- **HTTP tool invoke API** — `POST :18789/tools/invoke` lets external code (status-ui) call any gateway tool directly
- **Multi-agent routing** — single gateway process hosts multiple isolated agents, each with its own workspace and memory

**Service management:**
```bash
systemctl --user status openclaw-gateway
systemctl --user restart openclaw-gateway
journalctl --user -u openclaw-gateway -f
openclaw gateway health
```

---

### 2. Two agents: `personal` and `dev`

Both agents run inside the same openclaw gateway process. Memory and sessions are fully isolated per agent.

#### `personal` agent
- **Purpose:** General-purpose personal assistant
- **Sandbox:** off (runs tools on host)
- **Workspace:** `~/.openclaw/workspace/personal/`

#### `dev` agent
- **Purpose:** Autonomous software engineering — receives tasks from status-ui, plans, spawns coding workers, evaluates results
- **Sandbox:** `non-main` mode — all sub-agent sessions run inside Docker sandbox containers
- **Workspace:** `~/.openclaw/workspace/dev/`

**Agent workspace files** (read by the LLM on every session):

| File | Purpose |
|------|---------|
| `AGENTS.md` | Rules: code engine, worktree management, checkpointing thresholds, protected paths, conflict/test failure flows |
| `SOUL.md` | Agent identity and purpose |
| `MEMORY.md` | Auto-maintained rolling summary of past experience (openclaw writes this) |
| `memory/YYYY-MM-DD.md` | Daily memory logs — append-only, written by the agent during sessions |

---

### 3. Sandbox containers (`openclaw-sandbox:latest`)
**What it is:** Ephemeral Docker containers that run one coding worker each. Destroyed when the session ends.

**Image:** `openclaw-sandbox:latest` (built locally from `Dockerfile.sandbox`)
**Base:** `debian:bookworm-slim` + git + Node.js + `@anthropic-ai/claude-code`
**Lifetime:** One per sub-agent session (`scope: "session"`); auto-destroyed on session end
**Network:** `bridge` — workers reach the Anthropic API via Docker's default bridge; `host` mode is blocked by openclaw's sandbox security policy (since v2026.2.17)
**Why Docker:** `claude --dangerously-skip-permissions` requires container filesystem scope to limit blast radius

**Bind mounts into each sandbox container:**

| Host path | Container path | Mode | Purpose |
|-----------|---------------|------|---------|
| `~/10claude/workspace/projects` | `/app/workspace/projects` | `rw` | Project git repos |
| `~/10claude/workspace/worktrees` | `/app/workspace/worktrees` | `rw` | Per-worker git worktrees |
| `~/.claude` | `/root/.claude` | `rw` | Claude CLI auth credentials |

**Build:**
```bash
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox ~/10claude/
```

---

### 4. status-ui
**What it is:** FastAPI server that provides the Kanban web UI and acts as a thin proxy between the browser and the openclaw gateway.

**Location:** Docker container `status-ui`
**Port:** 18888
**Source:** `openclaw/tools/status_ui.py` + `openclaw/static/index.html`
**Resource limits:** 1 CPU, 512 MB RAM

**What it does:**
- Serves the Kanban board (`/`) and REST API (`/api/*`)
- Stores task metadata in `workspace/queue/task_store.json`
- Dispatches each task to `agent:dev:main` via `sessions_send` (synchronous call — blocks up to 10 min waiting for the agent's reply)
- Each task runs in its own background thread so the HTTP request returns immediately with `in_progress`
- Task marked `completed` or `failed` when the agent actually replies — no false positives from polling
- Stale-task watchdog: tasks stuck `in_progress` for >20 min after a service restart are auto-failed
- Writes outcomes + agent reply to `workspace/results/{task_id}.json`
- Generates plans using the LLM before task confirmation (plan/review/confirm flow)

**API endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Kanban board HTML |
| `GET` | `/api/tasks` | List all tasks with status |
| `GET` | `/api/projects` | List git projects in `workspace/projects/` |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/logs?tail=N` | Last N lines of `logs/daemon.log` |
| `POST` | `/api/plan` | Generate plan + questions, enter review state |
| `POST` | `/api/replan/{id}` | Regenerate plan with new feedback |
| `POST` | `/api/confirm/{id}` | Confirm plan → dispatch to openclaw |
| `POST` | `/api/submit` | Submit task directly (no plan review) |
| `POST` | `/api/retry/{id}` | Re-dispatch a failed task |
| `DELETE` | `/api/tasks/{id}` | Cancel a task |

**Kanban columns:**

| Column | `status` value | Meaning |
|--------|---------------|---------|
| Review | `review` | Plan generated, awaiting confirmation |
| Pending | `pending` | Confirmed, not yet dispatched |
| In Dev | `in_progress` | Dispatched to openclaw, worker running |
| Done | `completed` | Worker finished successfully |
| Failed | `failed` | Worker errored or dispatch failed |
| Cancelled | `cancelled` | Manually cancelled |

---

### 5. Caddy
**What it is:** Reverse proxy for TLS termination and basic auth.

**Location:** Docker container `caddy`
**Image:** `caddy:2-alpine` (pulled from Docker Hub, not built locally)
**Config:** `config/Caddyfile` (mounted read-only)
**Ports:** 80 (redirects to HTTPS), 443 (proxied to status-ui after auth)

Caddy only knows about `status-ui:18888`. The openclaw gateway port (18789) is not exposed through Caddy — it is loopback-only on the host.

---

### 6. scripts/worktree.sh
**What it is:** Shell helper that creates and removes git worktrees for parallel coding workers. Replaces the old Python `repo.py`.

**Location:** `scripts/worktree.sh`
**Called by:** Dev agent instructions (via bash tool inside sandbox containers)

```bash
# Create an isolated worktree for a worker
scripts/worktree.sh create <worker-id> /app/workspace/projects/<project>
# → creates /app/workspace/worktrees/<worker-id> on branch worker/<worker-id>
# → prints the worktree path

# Remove when done
scripts/worktree.sh remove <worker-id> /app/workspace/projects/<project>
```

---

### 7. config/CLAUDE.md
**What it is:** Agent rules that get committed into every new project repo. Claude Code reads it automatically when running in the project directory.

**Location:** `config/CLAUDE.md`
**Injected into:** every `workspace/projects/<name>/` repo on creation

Contains:
- Rebase failure flow (unstaged changes → conflicts → resolution steps)
- Test failure flow (run → analyze → fix → re-run → commit)
- Parallel worktree rules (no symlinks, edit PROGRESS.md via `git -C <repo-root>`)
- Lessons-learned format for PROGRESS.md (problem, solution, commit ID)

---

## Data Formats

### `workspace/queue/task_store.json`
Master task registry. All tasks live here from creation through completion.

```json
{
  "task-abc12345": {
    "id": "task-abc12345",
    "prompt": "Add a dark mode toggle to the settings page",
    "project_path": "my-project",
    "status": "in_progress",
    "created_at": "2026-02-19T10:00:00.123456",
    "confirmed_at": "2026-02-19T10:01:30.000000",
    "plan": "# Implementation Plan\n\n1. ...",
    "questions": [
      {
        "question": "Which storage for the preference?",
        "options": ["localStorage (Recommended)", "User DB column"]
      }
    ],
    "answers": {
      "Which storage for the preference?": "localStorage (Recommended)"
    },
    "depends_on": null,
    "openclaw_session_key": "agent:dev:main",
    "error": null
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | `task-<8 hex chars>` |
| `status` | string | `review` → `pending` → `in_progress` → `completed` / `failed` / `cancelled` |
| `plan` | string | Markdown plan generated before confirmation |
| `questions` | array | Clarifying questions shown in UI |
| `answers` | object | User's answers to questions |
| `openclaw_session_key` | string | Always `"agent:dev:main"` — the permanent dev agent session that receives tasks |
| `depends_on` | string\|null | ID of prerequisite task |

---

### `workspace/results/{task_id}.json`
Written by status-ui when the agent replies to the `sessions_send` call.

```json
{
  "task_id": "task-abc12345",
  "success": true,
  "reply": "Done ✅ Added greet() function, committed as e865184",
  "session_key": "agent:dev:main",
  "completed_at": "2026-02-19T10:45:00.000000"
}
```

The `reply` field contains the dev agent's actual response — visible in the Kanban UI when you expand a completed task.

---

### `~/.openclaw/openclaw.json`
openclaw gateway configuration. Managed partly by openclaw itself (do not hand-edit the `meta` or `gateway.auth.token` fields).

```json
{
  "gateway": {
    "mode": "local",
    "port": 18789,
    "auth": { "mode": "token", "token": "<auto-generated>" }
  },
  "agents": {
    "defaults": {
      "subagents": { "maxConcurrent": 3 },
      "sandbox": { "mode": "off" }
    },
    "list": [
      { "id": "personal", "name": "Personal Assistant",
        "workspace": "~/.openclaw/workspace/personal" },
      { "id": "dev", "name": "Dev Manager",
        "workspace": "~/.openclaw/workspace/dev",
        "sandbox": {
          "mode": "non-main", "scope": "session",
          "docker": {
            "image": "openclaw-sandbox:latest",
            "network": "bridge",
            "binds": ["...projects:rw", "...worktrees:rw", "...claude:rw"],
            "setupCommand": "git config --global user.email ..."
          }
        }
      }
    ]
  }
}
```

**Required fields that enable status-ui → dev agent dispatch** (must be present or tasks will fail):

```json
{
  "gateway": {
    "tools": { "allow": ["sessions_send", "sessions_spawn"] }
  },
  "tools": {
    "sessions": { "visibility": "all" },
    "agentToAgent": { "enabled": true }
  }
}
```

- `gateway.tools.allow` — whitelist `sessions_send` for HTTP `/tools/invoke` calls (blocked by default)
- `tools.sessions.visibility: "all"` — allow status-ui to address sessions in other agents
- `tools.agentToAgent.enabled: true` — enable agent-to-agent message routing

Key fields to change:
- `agents.defaults.subagents.maxConcurrent` — max parallel coding workers (default: 3)
- `gateway.port` — gateway port (default: 18789); also update status-ui env var if changed
- `agents.list[dev].sandbox.docker.binds` — update host paths if `~/10claude` moves

---

### `~/.openclaw/workspace/dev/memory/YYYY-MM-DD.md`
Daily memory log written by the dev agent during sessions. Append-only plain Markdown.
openclaw indexes these with BM25 + vector search (SQLite-vec at `~/.openclaw/agents/dev/agent/*.sqlite`).

```markdown
## 2026-02-19

### Task task-abc12345
Completed dark mode toggle. Used localStorage key `theme`. CSS variable approach
worked cleanly. Commit: a1b2c3d4

### Lesson learned
When adding CSS variables, always check existing Tailwind dark: classes first
to avoid duplication.
```

---

## Directory Structure

```
~/10claude/                              ← mounted as /app in Docker containers
│
├── openclaw/                            # Python source (status-ui + utilities)
│   ├── tools/
│   │   ├── status_ui.py                 # FastAPI kanban UI + openclaw proxy
│   │   └── project_manager.py           # create/list projects
│   └── static/index.html               # Kanban web UI (single HTML file)
│
├── config/                              # mounted read-only at /app/config
│   ├── CLAUDE.md                        # agent rules → committed into every project repo
│   └── Caddyfile                        # reverse proxy config
│
├── workspace/                           # ALL runtime state (back this up)
│   ├── queue/
│   │   └── task_store.json             # task metadata: id, prompt, status, plan, answers
│   ├── results/
│   │   └── {task_id}.json             # task outcome: success, session_key, completed_at
│   ├── projects/
│   │   └── {project-name}/            # isolated git repos; each contains CLAUDE.md
│   └── worktrees/
│       └── {worker-id}/               # ephemeral git worktrees (active workers only)
│
├── logs/
│   └── daemon.log                      # status-ui + container logs
│
├── backups/                            # manual/auto tarballs of workspace/
│
├── scripts/
│   ├── setup_10claude.sh              # bootstrap: dirs + images + containers
│   ├── migrate.sh                     # full migration to a new machine
│   ├── setup_tailscale.sh             # Tailscale remote access setup
│   └── worktree.sh                    # create/remove git worktrees for workers
│
├── Dockerfile                          # status-ui image (python:3.11-slim + claude CLI)
├── Dockerfile.sandbox                  # sandbox worker image (debian:bookworm-slim + claude CLI)
├── compose.yaml                        # 2 persistent services: caddy, status-ui
├── requirements.txt
├── setup.py
└── .env                               # secrets — never committed

~/.openclaw/                            # openclaw gateway state (back this up)
├── openclaw.json                       # gateway + agent config
├── workspace/
│   ├── personal/                       # personal agent workspace
│   │   ├── SOUL.md                    # agent identity
│   │   ├── MEMORY.md                  # rolling memory summary (auto-maintained)
│   │   └── memory/YYYY-MM-DD.md      # daily memory logs
│   └── dev/                           # dev agent workspace
│       ├── AGENTS.md                  # coding rules, worktree ops, checkpointing
│       ├── SOUL.md                    # agent identity
│       ├── MEMORY.md                  # rolling memory summary (auto-maintained)
│       └── memory/YYYY-MM-DD.md      # daily memory logs
└── agents/
    ├── personal/agent/                # personal agent auth + SQLite memory index
    └── dev/agent/                     # dev agent auth + SQLite memory index

~/.claude/                             # Claude CLI auth (device-bound, cannot be copied)
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | `docker compose version` to verify |
| Node.js ≥ 18 + npm | For openclaw and Claude CLI |
| `openclaw` npm package | `npm install -g openclaw` |
| Claude CLI authenticated | `claude auth login` — device-bound, required on each machine |
| Anthropic API key | For plan generation in status-ui (set in `.env`) |
| Git | For project repos and worktrees |
| 16 GB RAM recommended | 3 sandbox workers × ~3 GB each = ~9 GB peak |

---

## Fresh Install

### 1. Clone the repo

```bash
git clone <repo-url> ~/10claude
cd ~/10claude
```

### 2. Install Node.js and openclaw

```bash
# Node.js 20 via NodeSource
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# npm global prefix (non-root)
npm config set prefix ~/.npm-global
echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Claude CLI + openclaw
npm install -g @anthropic-ai/claude-code openclaw
```

### 3. Authenticate Claude CLI

```bash
claude auth login
# Follow the browser OAuth flow — required on every new machine
```

### 4. Create `.env`

```bash
cp .env.example .env   # or create manually
```

Required fields:

```dotenv
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

ANTHROPIC_API_KEY=sk-ant-...

# Web UI basic auth (default password: openclaw)
# Generate hash: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'
# Double every $ sign in the hash for Docker Compose compatibility
UI_USER=admin
UI_PASS_HASH=$$2a$$14$$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
```

### 5. Bootstrap

```bash
bash scripts/setup_10claude.sh
```

This installs the openclaw gateway systemd service, builds both Docker images, and starts all containers.

### 6. Verify

```bash
openclaw gateway health            # OK (0ms)
curl http://localhost:18888/api/health  # {"status":"ok","openclaw_url":"..."}
docker compose ps                  # caddy + status-ui both Up
```

---

## Configuration

### `~/.openclaw/openclaw.json` — gateway + agents

Primary config for the execution engine. Key fields:

| Field | Default | Description |
|-------|---------|-------------|
| `gateway.port` | `18789` | Gateway WebSocket + HTTP port |
| `gateway.tools.allow` | `[]` | Tools allowed via HTTP `/tools/invoke`; must include `"sessions_send"` |
| `tools.sessions.visibility` | `"self"` | Must be `"all"` for status-ui to reach dev agent sessions |
| `tools.agentToAgent.enabled` | `false` | Must be `true` for agent-to-agent message routing |
| `agents.defaults.subagents.maxConcurrent` | `3` | Max parallel coding workers per task |
| `agents.list[dev].sandbox.mode` | `non-main` | Sandbox all sub-agent (worker) sessions |
| `agents.list[dev].sandbox.scope` | `session` | One container per worker session |
| `agents.list[dev].sandbox.docker.network` | `bridge` | Internet access for Anthropic API (`host` blocked by openclaw security policy since v2026.2.17) |

After editing, restart the gateway:
```bash
systemctl --user restart openclaw-gateway
```

### `~/.openclaw/workspace/dev/AGENTS.md` — coding rules

Instructions injected into the dev agent's system prompt on every session. Edit this to change:
- Which tools workers use (`claude --dangerously-skip-permissions`)
- Worktree creation/cleanup steps
- Checkpointing thresholds
- Protected paths

### `config/CLAUDE.md` — project repo rules

Committed into every new project repo. Claude Code reads it automatically. Controls:
- Rebase and test failure recovery flows
- Parallel worktree conventions
- PROGRESS.md lessons-learned format

### `.env` — secrets

Never committed. Contains API keys and UI credentials.

---

## Web UI & Remote Access

### Local access

```
http://localhost:18888    direct, no auth (status-ui)
https://localhost         via Caddy, requires basic auth
```

### Remote access via Tailscale

```bash
bash scripts/setup_tailscale.sh
# Then open https://<tailscale-ip> on any device on your tailnet
```

### Changing the UI password

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yournewpassword'
# Paste output into .env as UI_PASS_HASH=$$2a$$...  (double every $)
docker compose restart caddy
```

---

## How to Use

### Submit a task (Plan mode — recommended)

1. Open `http://localhost:18888`
2. Type your task and enable **Plan Mode**
3. Click **Submit** — the LLM generates a plan and clarifying questions
4. Review the plan, answer the questions, click **Confirm**
5. The task moves to **In Dev** as the dev agent spawns coding workers
6. Watch it complete through the Kanban board

### Submit a task (direct)

Disable Plan Mode and click Submit. The task goes straight to the dev agent without a review step.

### Task dependencies

Set **Depends on** to another task's ID in the UI. The task will not start until its prerequisite reaches `completed`.

### Projects

Each task runs inside a **project** — an isolated git repo under `workspace/projects/`. Select an existing project or create a new one. The `CLAUDE.md` rules file is committed into every new project automatically.

### Check task logs

```bash
# All status-ui output
docker compose logs -f status-ui

# openclaw gateway (agent sessions, sandbox spawn)
journalctl --user -u openclaw-gateway -f

# Filter by task ID
grep task-abc12345 ~/10claude/logs/daemon.log
```

---

## Migration to a New Machine

### What the migration script automates

Run `bash scripts/migrate.sh` on the new machine after copying state. It handles:

- Installs Docker (if missing, via `get.docker.com`)
- Installs Node.js 20 (if missing or too old, via NodeSource)
- Installs Claude CLI + openclaw npm packages
- Validates copied state (`~/.openclaw/openclaw.json`, `.env`, workspace dirs)
- Reinstalls openclaw gateway as a systemd user service (`--force` to overwrite old unit)
- Rebuilds both Docker images (`status-ui` + `openclaw-sandbox:latest`)
- Clears any orphaned old containers (`openclaw`, `runtime-daemon`)
- Starts all containers
- Smoke-tests every component

### The one step that cannot be automated

```bash
claude auth login
```

Claude CLI auth is cryptographically bound to the device. It always requires an interactive browser flow on the new machine — there is no way to copy or skip this.

### How to move

```bash
# On the OLD machine — copy state to new host
rsync -av ~/10claude/   user@new-host:~/10claude/
rsync -av ~/.openclaw/  user@new-host:~/.openclaw/
rsync -av ~/.ssh/       user@new-host:~/.ssh/
scp ~/10claude/.env     user@new-host:~/10claude/.env

# On the NEW machine
bash ~/10claude/scripts/migrate.sh
claude auth login
```

After `claude auth login`, submit a small test task at `http://localhost:18888` to confirm sandbox containers spin up correctly.

---

## Recovery & Restore

### Full backup

```bash
# Captures all task history, results, projects, and openclaw memory
tar czf ~/openclaw-backup-$(date +%Y%m%d).tgz \
    ~/10claude/workspace \
    ~/10claude/logs \
    ~/10claude/config \
    ~/10claude/backups \
    ~/.openclaw/workspace \
    ~/.openclaw/agents
```

Does not include source code (in git) or secrets (`.env` — store separately).

### If containers fail to start

```bash
docker compose logs status-ui
docker compose logs caddy
journalctl --user -u openclaw-gateway -n 50

# Rebuild images
docker compose build --no-cache
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox .
docker compose up -d
```

### Read task logs

```bash
grep task-abc123 ~/10claude/logs/daemon.log
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| UI shows no tasks | `status-ui` container not running | `docker compose up -d status-ui` |
| Tasks stuck in Pending | openclaw gateway not running | `systemctl --user restart openclaw-gateway` |
| Tasks fail immediately | Claude CLI not authenticated | `claude auth login` (on host, not inside container) |
| `ANTHROPIC_API_KEY not set` | Missing from `.env` | Add key to `.env`, `docker compose up -d --force-recreate` |
| Caddy shows 502 Bad Gateway | `status-ui` not ready yet | Wait 15 s for pip install on startup |
| Self-signed cert warning | Expected on first visit | Accept/trust the cert once |
| Task failed — want to retry | Any failure | Click Retry in the UI or `POST /api/retry/{task_id}` |
| `UI_PASS_HASH` warnings | `$` not escaped in `.env` | Double every `$` → `$$` in the hash |
| openclaw gateway crashes on start | `gateway.mode` not set | Ensure `openclaw.json` has `"gateway": {"mode": "local", ...}` |
| Sandbox container fails to start | Image not built | `docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox .` |
| `maxConcurrent` schema error | Wrong config location | Field belongs in `agents.defaults.subagents`, not per-agent |
| Tasks complete instantly without doing any work | `sessions_send` misconfigured | Verify `openclaw.json` has `gateway.tools.allow: ["sessions_send"]`, `tools.sessions.visibility: "all"`, and `tools.agentToAgent: {"enabled": true}` |
| Tasks stuck in `in_progress` after service restart | Background threads lost on restart | Stale watchdog auto-fails them after 20 min; manually cancel and retry sooner |
| `sessions_send` error: "Either sessionKey or label is required" | Wrong arg field name | Use `"sessionKey": "agent:dev:main"` in the args (not `"to"`) |
| Plan generation unavailable | `ANTHROPIC_API_KEY` wrong type | OAuth tokens (`sk-ant-oat01-…`) are not accepted; set a standard API key (`sk-ant-api03-…`) in `.env` |
