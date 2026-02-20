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
      │  writes task_store.json → status: pending
      │
      │  (shared bind-mounted volume)
      ▼
   workspace/queue/task_store.json  ←─── polled every 5 s
      │
      │  flock atomic claim
      ├──────────────────────────────────┐
      │                │                 │
   [ worker-1 ]   [ worker-2 ]   [ worker-3 ]
   (6 CPU/6GB)    (6 CPU/6GB)    (6 CPU/6GB)
   [Docker, persistent, restart: unless-stopped]
      │
      │  claude --dangerously-skip-permissions -p '<task>'
      │  writes workspace/results/{task_id}.json
      ▼
   workspace/results/{task_id}.json
```

**Key design principle:** all state lives on the host (`~/10claude/workspace/`).
Containers are disposable — nothing is lost on restart or rebuild.

**Worker model:** three persistent Docker containers run continuously.
Each polls the shared task queue every 5 seconds, atomically claims one pending task
via `fcntl.flock`, executes it with the Claude CLI, and loops.
Workers are idle (sleeping) when the queue is empty; they never exit on their own.

---

## Components

### 1. Three persistent workers (`worker-1`, `worker-2`, `worker-3`)

**What they are:** Long-running Docker containers that each run `scripts/worker.py` — a Python polling loop that claims and executes tasks from the shared queue.

**Image:** `openclaw-sandbox:latest` (built locally from `Dockerfile.sandbox`)
**Base:** `debian:bookworm-slim` + git + Node.js + python3 + `@anthropic-ai/claude-code`
**Resource limits:** 6 CPUs, 6 GB RAM per worker
**User:** `worker` (uid 1000, non-root — required by claude CLI's `--dangerously-skip-permissions`)
**Restart:** `unless-stopped` — always running, managed by Docker

**How a worker picks up a task:**
1. Read `task_store.json`, find oldest `status: pending` task
2. Acquire exclusive `flock` on `workspace/queue/.queue.lock` (cross-container lock)
3. Mark task `in_progress`, write `worker_id`, release lock
4. Run `claude --dangerously-skip-permissions -p '<task prompt>'`
5. Write result to `workspace/results/{task_id}.json`
6. Update task `status` to `completed` or `failed`, release lock
7. Loop back to step 1

**Bind mounts into each worker container:**

| Host path | Container path | Mode | Purpose |
|-----------|---------------|------|---------|
| `~/10claude/workspace` | `/app/workspace` | `rw` | Task queue, results, project repos, worktrees |
| `~/10claude/scripts` | `/app/scripts` | `ro` | `worker.py` and helper scripts |
| `~/.claude` | `/home/worker/.claude` | `rw` | Claude CLI session data (cache, history) |
| `~/.claude.json` | `/home/worker/.claude.json` | `rw` | Claude CLI account metadata |
| `~/.ssh` | `/home/worker/.ssh` | `ro` | SSH keys for git push |

**Auth:** Claude CLI reads `~/.claude/.credentials.json` for its OAuth tokens.
This file is inside `~/.claude/` and is bind-mounted into the container.
After `claude auth login` on the host, all three workers authenticate automatically via this shared mount.

**Build:**
```bash
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox ~/10claude/
```

**Logs:**
```bash
docker compose logs -f worker-1
docker compose logs -f worker-2 worker-3
```

---

### 2. status-ui

**What it is:** FastAPI server that provides the Kanban web UI. Accepts tasks from the browser and writes them to the shared queue — workers pick them up independently.

**Location:** Docker container `status-ui`
**Port:** 18888
**Source:** `openclaw/tools/status_ui.py` + `openclaw/static/index.html`
**Resource limits:** 1 CPU, 512 MB RAM

**What it does:**
- Serves the Kanban board (`/`) and REST API (`/api/*`)
- Stores task metadata in `workspace/queue/task_store.json`
- **Goal decomposition:** breaks a high-level goal into 3–10 granular sub-tasks via Claude CLI; user reviews/refines interactively before approving
- On submit/approve: writes tasks with `status: pending` — workers claim within 5 seconds
- Dependency enforcement: tasks with `depends_on` are not claimed until all prerequisites are `completed`
- Stale-task watchdog: tasks stuck `in_progress` for >20 min (worker crash) are auto-failed; goals auto-complete when all sub-tasks finish
- Reads results from `workspace/results/{task_id}.json` to show outcomes in the UI
- Fixes queue file permissions on startup so non-root workers can read/write them

**API endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Kanban board HTML |
| `GET` | `/api/tasks` | List all tasks and goals with status |
| `GET` | `/api/projects` | List git projects in `workspace/projects/` |
| `POST` | `/api/projects` | Create a new git-initialised project |
| `GET` | `/api/health` | Health check (lists worker names) |
| `GET` | `/api/logs?tail=N` | Last N lines of `logs/daemon.log` |
| `POST` | `/api/decompose` | Decompose a goal into sub-tasks (review state) |
| `POST` | `/api/refine/{goal_id}` | Refine sub-tasks with feedback, re-decompose |
| `POST` | `/api/approve/{goal_id}` | Approve goal → all draft tasks become pending |
| `POST` | `/api/submit` | Submit a single task directly (no decomposition) |
| `POST` | `/api/plan` | Generate plan + questions (legacy single-task flow) |
| `POST` | `/api/replan/{id}` | Regenerate plan with new feedback |
| `POST` | `/api/confirm/{id}` | Confirm plan → enqueue for a worker |
| `POST` | `/api/retry/{id}` | Re-queue a failed task |
| `DELETE` | `/api/tasks/{id}` | Cancel a task or goal |

**Kanban columns (Status tab — read-only):**

| Column | `status` value | Meaning |
|--------|---------------|---------|
| Pending | `pending` | Queued, worker will claim within 5 s (deps met) |
| In Dev | `in_progress` | Claimed by a worker, Claude CLI running |
| Done | `completed` | Worker finished successfully |
| Failed | `failed` | Worker errored or timed out |
| Cancelled | `cancelled` | Manually cancelled |

Goals (`type: "goal"`) are shown in the **Plan tab** only, not in the kanban. The Plan tab shows goals with `review` or `approved` status for user interaction.

---

### 3. openclaw gateway (optional, personal assistant only)

**What it is:** The [openclaw](https://openclaw.ai) npm package — an AI messaging and agent orchestration gateway. In the current architecture it is used for the `personal` assistant agent, **not** for task dispatch (workers handle that directly via the queue).

**Location:** Native host process, not in Docker.
**Binary:** `~/.npm-global/bin/openclaw`
**Config:** `~/.openclaw/openclaw.json`
**Managed by:** systemd user service `openclaw-gateway`
**Port:** 18789

**Note on docker access:** The systemd user service wraps the node command with `sg docker` so the gateway process runs with the `docker` group and can manage sandbox containers (systemd user services cannot use `SupplementaryGroups`).

**Service management:**
```bash
systemctl --user status openclaw-gateway
systemctl --user restart openclaw-gateway
journalctl --user -u openclaw-gateway -f
openclaw gateway health
```

---

### 4. Caddy

**What it is:** Reverse proxy for TLS termination and basic auth.

**Location:** Docker container `caddy`
**Image:** `caddy:2-alpine` (pulled from Docker Hub, not built locally)
**Config:** `config/Caddyfile` (mounted read-only)
**Ports:** 80 (redirects to HTTPS), 443 (proxied to status-ui after auth)

---

### 5. scripts/worker.py

**What it is:** The polling worker loop. Runs inside each `worker-1/2/3` container.

**Location:** `scripts/worker.py`
**Language:** Python 3 (no dependencies beyond stdlib)

Key behaviour:
- `fcntl.flock` on `workspace/queue/.queue.lock` for cross-container atomic claiming
- Sorts pending tasks by `created_at` — oldest first (FIFO)
- Skips `type: "goal"` entries (never claimed by workers)
- Skips tasks whose `depends_on` prerequisites are not yet `completed`
- Runs `claude --dangerously-skip-permissions -p '<prompt>'` with a 600 s timeout
- For goal sub-tasks: appends a mandatory `git add -A && git pull --rebase && git commit && git push origin main` step (with push-retry on rejection) to every prompt
- Writes result JSON and updates `task_store.json` atomically
- Never exits — loops and sleeps 5 s when the queue is empty

---

### 6. scripts/worktree.sh

**What it is:** Shell helper for creating and removing git worktrees for parallel sub-tasks within a single coding job.

```bash
# Create a worktree (from the host — main session context)
scripts/worktree.sh create <worker-id> /path/to/project
# prints: host:/home/wangk/10claude/workspace/worktrees/<worker-id>
#         container:/app/workspace/worktrees/<worker-id>

# Remove when done
scripts/worktree.sh remove <worker-id> /path/to/project
```

---

### 7. config/CLAUDE.md

**What it is:** Agent rules committed into every new project repo. Claude Code reads it automatically when working in the project directory.

Contains:
- Rebase failure flow (unstaged changes → conflicts → resolution steps)
- Test failure flow (run → analyze → fix → re-run → commit)
- Parallel worktree rules
- Lessons-learned format for PROGRESS.md

---

## Data Formats

### `workspace/queue/task_store.json`

Master task registry. Written by status-ui, read+written by workers. Contains both **goal** entries and **task** entries.

**Goal entry** (`type: "goal"`):
```json
{
  "goal-abc12345": {
    "id": "goal-abc12345",
    "type": "goal",
    "prompt": "Build a REST API with JWT auth and tests",
    "project_path": "my-project",
    "status": "approved",
    "created_at": "2026-02-20T10:00:00.123456",
    "task_ids": ["task-001", "task-002", "task-003"],
    "feedback_history": ["use pytest fixtures, not bare assert"]
  }
}
```

Goal status lifecycle: `review` → `approved` → `completed` / `cancelled`

**Task entry** (`type: "task"`, sub-task of a goal):
```json
{
  "task-001": {
    "id": "task-001",
    "type": "task",
    "goal_id": "goal-abc12345",
    "title": "Set up FastAPI project structure",
    "prompt": "In /app/workspace/projects/my-project, create main.py...",
    "acceptance_criteria": "cd /app/workspace/projects/my-project && python -m pytest tests/ -v",
    "project_path": "my-project",
    "status": "completed",
    "depends_on": [],
    "created_at": "2026-02-20T10:00:00.123456",
    "worker_id": "worker-1"
  }
}
```

Task status lifecycle: `draft` → `pending` (on goal approval) → `in_progress` → `completed` / `failed`

**Standalone task entry** (direct submit, no goal):
```json
{
  "task-abc12345": {
    "id": "task-abc12345",
    "prompt": "Add a dark mode toggle to the settings page",
    "project_path": "my-project",
    "status": "completed",
    "created_at": "2026-02-20T10:00:00.123456",
    "worker_id": "worker-2",
    "depends_on": null
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | `goal-<8 hex>` for goals; `task-<8 hex>` for tasks |
| `type` | string | `"goal"` or `"task"` (omitted for legacy standalone tasks) |
| `status` | string | See lifecycle above |
| `goal_id` | string | Parent goal ID (task entries only) |
| `task_ids` | array | Child task IDs (goal entries only) |
| `depends_on` | list\|null | List of prerequisite task IDs (workers skip until all are `completed`) |
| `worker_id` | string | Which worker claimed this task |
| `feedback_history` | array | User feedback strings used in re-decomposition |

---

### `workspace/results/{task_id}.json`

Written by the worker when the Claude CLI finishes.

```json
{
  "task_id": "task-abc12345",
  "success": true,
  "reply": "Done. Created dark mode toggle with localStorage. Committed as a1b2c3d.",
  "worker_id": "worker-2",
  "completed_at": "2026-02-20T10:08:12.000000"
}
```

The `reply` field contains the full stdout output from the Claude CLI — visible in the Kanban UI when you expand a completed task.

---

### `~/.openclaw/openclaw.json`

openclaw gateway configuration (personal assistant agent only). Managed partly by openclaw itself.

```json
{
  "gateway": {
    "mode": "local",
    "port": 18789,
    "auth": { "mode": "token", "token": "<auto-generated>" },
    "tools": { "allow": ["sessions_send", "sessions_spawn"] }
  },
  "agents": {
    "list": [
      { "id": "personal", "name": "Personal Assistant",
        "workspace": "~/.openclaw/workspace/personal" },
      { "id": "dev", "name": "Dev Manager",
        "workspace": "~/.openclaw/workspace/dev",
        "sandbox": {
          "mode": "non-main",
          "docker": { "image": "openclaw-sandbox:latest", "network": "bridge" }
        }
      }
    ]
  }
}
```

---

## Directory Structure

```
~/10claude/                              ← task execution root
│
├── openclaw/                            # Python source (status-ui)
│   ├── tools/
│   │   ├── status_ui.py                 # FastAPI kanban UI + queue writer
│   │   └── project_manager.py           # create/list projects
│   └── static/index.html               # Kanban web UI (single HTML file)
│
├── config/                              # static config
│   ├── CLAUDE.md                        # agent rules → committed into every project
│   └── Caddyfile                        # reverse proxy config
│
├── workspace/                           # ALL runtime state (back this up)
│   ├── queue/
│   │   ├── task_store.json             # task metadata: id, prompt, status, worker_id
│   │   └── .queue.lock                 # flock file for cross-container atomic claims
│   ├── results/
│   │   └── {task_id}.json             # task outcome: success, reply, worker_id
│   ├── projects/
│   │   └── {project-name}/            # isolated git repos; each contains CLAUDE.md
│   └── worktrees/
│       └── {worker-id}/               # ephemeral git worktrees (active sub-tasks only)
│
├── logs/
│   └── daemon.log                      # status-ui logs
│
├── backups/                            # manual/auto tarballs of workspace/
│
├── scripts/
│   ├── migrate.sh                     # full install / migration to a new machine
│   ├── setup_tailscale.sh             # Tailscale remote access setup
│   ├── worker.py                      # persistent worker polling loop (runs in containers)
│   └── worktree.sh                    # create/remove git worktrees for sub-tasks
│
├── Dockerfile                          # status-ui image (python:3.11-slim)
├── Dockerfile.sandbox                  # worker image (debian:bookworm-slim + claude CLI)
│                                       # Includes: git, nodejs, npm, python3,
│                                       #           @anthropic-ai/claude-code
│                                       # Non-root user: worker (uid 1000)
├── compose.yaml                        # 5 services: caddy, status-ui, worker-1/2/3
├── requirements.txt
├── setup.py
└── .env                               # secrets — never committed

~/.openclaw/                            # openclaw gateway state (personal agent)
├── openclaw.json                       # gateway + agent config
└── workspace/
    ├── personal/                       # personal agent workspace
    └── dev/                           # dev agent workspace (AGENTS.md, SOUL.md)

~/.claude/                             # Claude CLI auth — device-bound
│   .credentials.json                  # ← OAuth tokens — bind-mounted into workers
│                                      #   workers authenticate via this shared file
│   .claude.json                       # account metadata (no tokens)
└── ...
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | `docker compose version` to verify |
| Node.js ≥ 18 + npm | For openclaw and Claude CLI |
| `openclaw` npm package | `npm install -g openclaw` |
| Claude CLI authenticated | `claude auth login` — device-bound, required on each machine |
| Anthropic API key | For plan generation in status-ui (set `ANTHROPIC_API_KEY` in `.env`) |
| Git | For project repos and worktrees |
| 20 GB RAM recommended | 3 workers × 6 GB each + OS + status-ui |

---

## Fresh Install

### 1. Clone the repo

```bash
git clone <repo-url> ~/10claude
cd ~/10claude
```

### 2. Run the bootstrap script

```bash
bash scripts/migrate.sh
```

This single script handles everything:
- Installs Docker, Node.js if missing
- Installs Claude CLI + openclaw npm packages
- Creates `.env` template if not present (edit it, then re-run)
- Creates workspace directories
- Injects required keys into `~/.openclaw/openclaw.json`
- Installs + starts the openclaw gateway as a systemd user service (with `sg docker` wrapper for container management)
- Builds both Docker images (`status-ui` + `openclaw-sandbox:latest` with python3 and non-root `worker` user)
- Starts all containers (`caddy`, `status-ui`, `worker-1`, `worker-2`, `worker-3`)
- Smoke-tests every component

### 3. Authenticate Claude CLI

```bash
claude auth login
# Follow the browser OAuth flow
```

This is the **one step that cannot be automated**. Claude CLI auth is device-bound.
After login, `~/.claude/.credentials.json` is updated automatically and all three worker containers authenticate via this shared bind-mount — no extra steps needed.

### 4. Verify

```bash
curl http://localhost:18888/api/health
# → {"status":"ok","workers":["worker-1","worker-2","worker-3"]}

docker compose ps
# NAME        STATUS
# caddy       Up
# status-ui   Up
# worker-1    Up
# worker-2    Up
# worker-3    Up

docker compose logs --tail=3 worker-1 worker-2 worker-3
# [HH:MM:SS] [worker-1] Worker started. Polling every 5s for pending tasks...
# [HH:MM:SS] [worker-2] Worker started. Polling every 5s for pending tasks...
# [HH:MM:SS] [worker-3] Worker started. Polling every 5s for pending tasks...
```

---

## Configuration

### `.env` — secrets and git identity

```dotenv
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

# Used by status-ui for plan generation (Anthropic API)
# Use a standard API key (sk-ant-api03-...) NOT an OAuth token
ANTHROPIC_API_KEY=sk-ant-api03-...

# Host user IDs — workers chown newly created project repos to avoid root-owned files
HOST_UID=1000
HOST_GID=1000

# Web UI basic auth (default password: openclaw)
# Generate hash: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'
# Double every $ sign in the hash for Docker Compose compatibility
UI_USER=admin
UI_PASS_HASH=$$2a$$14$$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm

# GitHub integration — create private repos and push commits automatically
# Get your token: gh auth token   (needs 'repo' scope)
GITHUB_TOKEN=ghp_...
# Your GitHub username (used as org for new repos)
GITHUB_USER=your-github-username
```

**Note on auth:**
- `ANTHROPIC_API_KEY` is **not used** for goal decomposition — status-ui and workers both call the `claude` CLI directly, which uses `~/.claude/.credentials.json` (the OAuth session from `claude auth login`). `ANTHROPIC_API_KEY` is only needed by the legacy `/api/plan` Python SDK path.
- `GITHUB_TOKEN` needs the `repo` scope. Get it with `gh auth token` after `gh auth login`. It's used by status-ui to create GitHub repos and by workers for `git push`.
- Workers authenticate to Claude via `~/.claude/.credentials.json`; run `claude auth login` on the host — all containers pick it up automatically via bind-mount.

### `scripts/worker.py` — worker behaviour

| Constant | Default | Description |
|----------|---------|-------------|
| `POLL_SECS` | `5` | Seconds to sleep when queue is empty |
| `TASK_TIMEOUT` | `600` | Per-task timeout in seconds (10 min) |

### `compose.yaml` — resource limits

Each worker is defined identically with:
```yaml
deploy:
  resources:
    limits:
      cpus: "6"
      memory: 6G
```

Change these to match your hardware. With 3 workers at 6 CPUs each, you need at least 18 logical CPU cores to avoid throttling.

### `~/.openclaw/workspace/dev/AGENTS.md` — dev agent rules

Instructions for the `dev` openclaw agent (personal assistant context). Not used by the queue workers.

### `config/CLAUDE.md` — project repo rules

Committed into every new project repo. Claude Code reads it automatically. Controls rebase/test failure recovery and worktree conventions.

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

### Goal decomposition (recommended for any multi-step work)

The UI has two tabs: **✦ Plan** (for composition and review) and **◎ Status** (read-only kanban).

1. Open `http://localhost:18888` — the **Plan** tab is shown by default
2. Type your high-level goal in the text area
3. Select an existing project pill (sorted by most recently used) or click **＋ New project** to create one (a private GitHub repo is created automatically)
4. Click **✦ Decompose Goal**
5. A **planning modal** appears with 3–5 clarifying questions (tech stack, API style, testing approach, etc.) — select an option for each, then click **Decompose with these choices**
6. Claude breaks the goal into 3–10 granular sub-tasks with dependency ordering, shown as a goal card below the compose area
7. Read each task's title, prompt, and acceptance criteria
8. Optionally type feedback and click **✎ Refine** to re-generate with your notes (repeat as needed)
9. Click **✓ Approve & Start** — all sub-tasks become `pending`; workers pick them up in dependency order
10. Each worker commits and **pushes to GitHub** after finishing; if a parallel worker pushed first, it retries with `pull --rebase`

### Submit a single task (direct, no decomposition)

Type the task, select a project from the **Project** dropdown, and click **Add**. The task goes straight to the queue as `pending`.

### Parallel execution

Up to 3 tasks run simultaneously (one per worker). Tasks beyond 3 wait in `pending`. Goal sub-tasks with `depends_on` are only claimed after all their prerequisites reach `completed`.

### Task dependencies

Goal sub-tasks automatically get `depends_on` set by the decomposer. For manual tasks, set **Prerequisite** to an existing completed task's ID — the new task will not be claimed until that task completes.

### Check worker logs

```bash
# Live logs from all workers
docker compose logs -f worker-1 worker-2 worker-3

# Filter by task ID
docker compose logs worker-1 | grep task-abc12345

# Status-ui logs (plan generation, health checks)
docker compose logs -f status-ui
```

---

## Migration to a New Machine

### What the migration script automates

Run `bash scripts/migrate.sh` on the new machine after copying state. It handles:

- Installs Docker (if missing, via `get.docker.com`)
- Installs Node.js 20 (if missing or too old, via NodeSource)
- Installs Claude CLI + openclaw npm packages
- Validates copied state (`~/.openclaw/openclaw.json`, `.env`, workspace dirs)
- Reinstalls openclaw gateway as a systemd user service with `sg docker` wrapper
- Rebuilds both Docker images (`status-ui` + `openclaw-sandbox:latest`)
- Clears any orphaned old containers
- Starts all containers (`caddy`, `status-ui`, `worker-1/2/3`)
- Smoke-tests every component

### The one step that cannot be automated

```bash
claude auth login
```

Claude CLI auth is cryptographically bound to the device. It always requires an interactive browser flow on the new machine. After login, `~/.claude/.credentials.json` is populated and all workers authenticate automatically via the shared bind-mount.

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
gh auth login   # needed for GitHub repo creation (gh auth token → GITHUB_TOKEN in .env)
```

After `claude auth login`, submit a small test task at `http://localhost:18888` to confirm all workers pick up tasks correctly.

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

### If workers fail to start

```bash
# Check logs for the error
docker compose logs worker-1

# Common fixes
docker compose up -d --force-recreate worker-1 worker-2 worker-3

# If image is stale, rebuild
docker build -t openclaw-sandbox:latest -f Dockerfile.sandbox .
docker compose up -d --force-recreate worker-1 worker-2 worker-3
```

### If status-ui fails to start

```bash
docker compose logs status-ui
docker compose build --no-cache status-ui
docker compose up -d status-ui
```

### Queue file permission errors

If workers log `Permission denied: '/app/workspace/queue/.queue.lock'`, the queue files are root-owned (created by the old container). Restart status-ui — it fixes permissions on startup:

```bash
docker restart status-ui
```

### Re-authenticate workers

Workers use `~/.claude/.credentials.json` automatically. If they report "Not logged in":

```bash
claude auth login   # on the HOST (not inside a container)
# Workers immediately use the refreshed credentials — no restart needed
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Workers log "Not logged in" | Claude CLI session expired | `claude auth login` on host |
| Workers log "Permission denied: .queue.lock" | Root-owned queue files from old container | `docker restart status-ui` (fixes permissions on startup) |
| Tasks stuck in Pending | All workers busy or crashed | Check `docker compose ps` — restart crashed workers |
| Task with `depends_on` not starting | Prerequisite not yet `completed` | Normal — it will start automatically once the dependency finishes |
| Task failed immediately | Worker timeout (>10 min) or claude error | Check `docker compose logs worker-N`; retry or simplify the task |
| UI shows no tasks | `status-ui` container not running | `docker compose up -d status-ui` |
| Caddy shows 502 Bad Gateway | `status-ui` not ready yet | Wait 15 s for pip install on startup |
| Self-signed cert warning | Expected on first visit | Accept/trust the cert once |
| `UI_PASS_HASH` warnings | `$` not escaped in `.env` | Double every `$` → `$$` in the hash |
| Decompose returns "no tasks generated" | Claude CLI auth issue in status-ui | Run `docker exec status-ui claude -p "hi" --output-format json` — if it fails, check `~/.claude/.credentials.json` and run `claude auth login` on the host |
| openclaw gateway crashes | `sg docker` wrapper missing | Check service file: `cat ~/.config/systemd/user/openclaw-gateway.service` |
| `docker compose logs` shows worker restarting in a loop | worker.py crashing on start | Usually a Python error — run `docker compose logs worker-1` to see traceback |
| Tasks completed but no git commits in project | Worker didn't commit | Each goal sub-task prompt includes commit + push steps; check task result for git errors |
| Tasks completed but not pushed to GitHub | Push rejected or no remote | Worker retries push once after `pull --rebase`; check task result for push errors |
| `git push` rejected (non-fast-forward) | Parallel worker pushed first | Worker automatically retries: `pull --rebase` then push again |
| New project not appearing on GitHub | `GITHUB_TOKEN` missing or wrong scope | Run `gh auth token` on host and set `GITHUB_TOKEN` in `.env`; token needs `repo` scope |
| Tasks completed but files not in project | Worker used wrong project path | Check task prompt includes correct path; workers look in `/app/workspace/projects/<name>/` |
| Newly created projects owned by root | `HOST_UID`/`HOST_GID` not set | Add `HOST_UID=1000` and `HOST_GID=1000` to `.env`, recreate status-ui |
