# OpenClaw — Autonomous Dev Manager

OpenClaw is a self-hosted autonomous software engineering system.
You describe what you want done; the system plans, executes, and evaluates the work using Claude as its coding engine — without human intervention.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Fresh Install](#fresh-install)
4. [Configuration](#configuration)
5. [Web UI & Remote Access](#web-ui--remote-access)
6. [How to Use](#how-to-use)
7. [Directory Structure](#directory-structure)
8. [Containers](#containers)
9. [Recovery & Restore](#recovery--restore)
10. [Troubleshooting](#troubleshooting)

---

## Architecture

```
You (browser / CLI)
      │
      │  HTTPS :443
      ▼
   [ Caddy ]  — TLS termination + basic auth
      │
      │  HTTP :18888
      ▼
   [ openclaw ]  — FastAPI control plane + Kanban UI
      │  writes task to filesystem queue
      ▼
   /app/workspace/queue/tasks.jsonl
      │
      │  polls every 3 s
      ▼
   [ runtime-daemon ]  — execution plane
      │
      ├─ hierarchical_planner  → sub-goals with done_when criteria
      │
      └─ for each sub-goal: ◄──────────────────────────────┐
            │                                                │
            ├─ dag_planner      → DAG of parallel tasks     │
            ├─ parallel_executor → up to 3 workers          │
            │     each worker gets isolated git worktree    │
            │     each worker invokes Claude CLI            │
            ├─ goal_evaluator   → did done_when pass?       │
            ├─ if major change  → git checkpoint + snapshot │
            └─ advance to next sub-goal ─────────────────────┘
      │
      ▼
   /app/workspace/results/{task_id}.json
```

**Key design principle:** containers are disposable — all state lives in `~/10claude` on the host, mounted as `/app` inside every container. Nothing is lost on restart.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose v2 | `docker compose version` to verify |
| Claude CLI authenticated | `claude --version` — must be logged in (`claude auth login`) |
| Anthropic API key | For LLM planning calls (not the same as Claude CLI auth) |
| Git | For automatic code checkpoints |
| 32 GB RAM, 24 CPU host (recommended) | Daemon is limited to 20 GB / 15 CPUs |

---

## Fresh Install

### 1. Clone the repo

```bash
git clone <repo-url> ~/10claude
cd ~/10claude
```

### 2. Create `.env`

```bash
cp .env.example .env   # if it exists, otherwise create manually
```

`.env` must contain:

```dotenv
# Git identity used for auto-commits inside containers
GIT_AUTHOR_NAME=your-name
GIT_AUTHOR_EMAIL=your@email.com
GIT_COMMITTER_NAME=your-name
GIT_COMMITTER_EMAIL=your@email.com

# Anthropic API key (for LLM planning)
ANTHROPIC_API_KEY=sk-ant-...

# OpenAI API key (optional, for Codex fallback)
OPENAI_API_KEY=sk-...

# Web UI basic auth (default password: openclaw — change this!)
# Generate a new hash: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'
UI_USER=admin
UI_PASS_HASH=$$2a$$14$$OjGuIIHJeAHNMNt8UstDwO44MAX3tFsdsmX/5Y7yIV28xV9ZhDWUm
```

> Note: `$` signs in `UI_PASS_HASH` must be doubled (`$$`) — Docker Compose treats `$` as a variable prefix.

### 3. Run the bootstrap script

```bash
bash scripts/setup_10claude.sh
```

This creates required directories, builds images, and starts all containers.

### 4. Verify

```bash
docker compose ps          # all 3 containers should be Up
curl http://localhost:18888/api/health   # {"status":"ok","running":null}
```

---

## Configuration

All config lives in `config/` — mounted read-only into containers at `/app/config`.

### `config/llm_backends.yaml` — LLM provider

```yaml
default_provider: anthropic   # anthropic | openai | local
providers:
  anthropic:
    model: claude-3-5-sonnet-20241022
  openai:
    model: gpt-4o-mini
```

Change `default_provider` to switch which LLM handles planning.

### `config/coding_agents.yaml` — Code engine

```yaml
default: claude   # claude | codex
```

`claude` = Claude CLI (recommended). `codex` = OpenAI GPT-4o via API.

### `config/runtime_parallelism.yaml` — Worker pool

```yaml
parallelism:
  max_workers: 3      # concurrent workers per task
  per_task_limit: 1   # tasks running simultaneously
```

Each worker spawns a `claude` CLI process. 3 workers × ~3 GB each = ~9 GB peak RAM.

### `config/major_change.yaml` — Auto-checkpoint thresholds

```yaml
thresholds:
  files_changed: 5    # git diff --stat threshold
  insertions: 50
  deletions: 20

always_major:
  - "config/"
  - "database schema"
  - "migration"
```

When a sub-goal crosses these thresholds, the system commits and snapshots before continuing.

### `config/architecture.yaml` — Protected paths

```yaml
protected_paths:
  - "/app/config"
  - "/app/workspace/memory"
  - "/app/workspace/semantic_memory"
```

The planner will not schedule writes to these paths.

### `config/CLAUDE.md` — Agent rules

Copied into every new project repo on creation. Claude Code reads this automatically from the working directory. Documents:

- Conflict resolution flow (rebase failures, test failures)
- Parallel worktree architecture rules
- PROGRESS.md lessons-learned format

### `.env` — Secrets

Never committed to git. Contains API keys and UI credentials. See [Fresh Install](#fresh-install) for full template.

---

## Web UI & Remote Access

### Local access

```
http://localhost:18888    (direct, no auth)
https://localhost         (via Caddy, requires basic auth)
```

### Remote access via Tailscale

1. Install Tailscale on this machine:
   ```bash
   bash scripts/setup_tailscale.sh
   ```

2. Install Tailscale on your phone (same account).

3. Open `https://<tailscale-ip>` on your phone.
   - Accept the self-signed cert warning once.
   - Login: `admin` / `openclaw` (change in `.env`).

### Changing the UI password

```bash
# Generate a new bcrypt hash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yournewpassword'

# Paste the output into .env — double every $ sign:
# UI_PASS_HASH=$$2a$$14$$...
```

Then restart Caddy:
```bash
docker compose restart caddy
```

### Kanban board columns

| Column | Meaning |
|--------|---------|
| Pending | Queued, waiting for daemon |
| In Dev | Daemon is actively running it |
| Review | Plan generated, awaiting your confirmation |
| Done | Completed successfully |
| Failed | Completed with errors — retry available |
| Cancelled | Manually cancelled |

---

## How to Use

### Via the Web UI (recommended)

1. Open `http://localhost:18888` (or Tailscale URL).
2. Type your task in the input box.
3. Enable **Plan Mode** to have Claude generate a plan and ask clarifying questions before executing.
4. Click **Submit** (or Cmd+Enter).
5. Watch the task move through the Kanban columns.

### Via the CLI (inside the container)

```bash
docker exec -it openclaw openclaw
```

Prompts for project selection, then task input. Streams live log output until the task completes.

### Projects

Each task runs inside a **project** — an isolated git repository under `/app/workspace/projects/`. On first use you create a project; subsequent tasks can continue an existing one. Agent rules from `config/CLAUDE.md` are automatically committed into each new project.

### Task dependencies

In the UI, set **Depends on** to another task's ID. The daemon will not start the dependent task until its prerequisite completes successfully.

---

## Directory Structure

```
~/10claude/                         ← mounted as /app in all containers
│
├── openclaw/                       # Python source (control + execution plane)
│   ├── adapters/
│   │   ├── coder/                  # claude_cli.py, codex_cli.py
│   │   ├── git/                    # repo.py — worktree management
│   │   └── llm/                    # anthropic.py, openai.py, client.py
│   ├── tools/
│   │   ├── runtime_engine.py       # daemon main loop
│   │   ├── status_ui.py            # FastAPI + Kanban UI API
│   │   ├── long_horizon_loop.py    # outer planning → sub-goals loop
│   │   ├── hierarchical_planner.py # goal decomposition
│   │   ├── dag_planner.py          # sub-goal → DAG
│   │   ├── parallel_executor.py    # concurrent DAG runner
│   │   ├── coding_agent_router.py  # dispatch to Claude/Codex per worktree
│   │   ├── goal_evaluator.py       # done_when criterion evaluation
│   │   ├── progress_memory.py      # git checkpoints + workspace snapshots
│   │   ├── project_manager.py      # create/list projects
│   │   ├── semantic_memory.py      # long-term experience store
│   │   ├── semantic_retrieval.py   # experience retrieval
│   │   └── world_model.py          # persistent world model (JSON)
│   ├── static/index.html           # Kanban web UI (single file)
│   └── cli.py                      # interactive CLI entrypoint
│
├── config/                         # mounted read-only at /app/config
│   ├── llm_backends.yaml           # LLM provider + model
│   ├── coding_agents.yaml          # code engine (claude / codex)
│   ├── runtime_parallelism.yaml    # worker pool size
│   ├── major_change.yaml           # auto-checkpoint thresholds
│   ├── architecture.yaml           # protected paths
│   ├── CLAUDE.md                   # agent rules (copied into new projects)
│   └── Caddyfile                   # reverse proxy + TLS + basic auth
│
├── workspace/                      # ALL runtime state
│   ├── queue/
│   │   ├── tasks.jsonl             # append-only task queue
│   │   ├── task_store.json         # full task metadata (plan, answers, status)
│   │   └── current_task            # task ID currently running (read by UI)
│   ├── results/                    # {task_id}.json — outcome of each task
│   ├── projects/                   # isolated git repos per project
│   │   └── {project-name}/         # each is a full git repo + CLAUDE.md
│   ├── worktrees/                  # git worktrees (one per active worker)
│   ├── memory/
│   │   └── world_model.json        # agent's persistent world model
│   ├── semantic_memory/
│   │   └── entries/                # timestamped experience JSON files
│   └── repo/                       # legacy fallback repo (no project selected)
│
├── logs/
│   └── daemon.log                  # unified log — grep <task-id> to filter
│
├── backups/                        # auto-snapshots (tarballs of /app/workspace)
├── scripts/
│   ├── setup_10claude.sh           # bootstrap: dirs + build + start
│   └── setup_tailscale.sh          # install Tailscale for remote access
├── tests/                          # pytest test suite
├── compose.yaml                    # 3 services: caddy, openclaw, runtime-daemon
├── Dockerfile                      # single image for openclaw + runtime-daemon
├── requirements.txt
├── setup.py
└── .env                            # secrets — never committed
```

---

## Containers

### `caddy`

Reverse proxy. Terminates TLS, enforces basic auth, forwards to `openclaw:18888`.
Image: `caddy:2-alpine` (pulled from Docker Hub, not built locally).

- Port 80 → redirects to HTTPS
- Port 443 → proxied to openclaw after auth

### `openclaw`

Control plane + web UI.

- FastAPI server on port 18888
- Serves the Kanban board at `/`
- API at `/api/*`
- Resource limits: 1 CPU, 512 MB RAM

### `runtime-daemon`

Execution plane. Never directly user-facing.

- Polls `/app/workspace/queue/tasks.jsonl` every 3 seconds
- Spawns up to 3 Claude CLI workers per task
- Writes results to `/app/workspace/results/`
- Resource limits: 15 CPUs, 20 GB RAM

All three containers mount `~/10claude` as `/app`.

---

## Recovery & Restore

### Full backup

```bash
cd ~/10claude
tar czf ~/openclaw-backup-$(date +%Y%m%d).tgz \
    workspace logs config backups
```

This captures task history, results, agent memory, semantic memory, and all config. Does not include source code (that's in git).

### Restore on a new machine

```bash
# 1. Clone the repo
git clone <repo-url> ~/10claude
cd ~/10claude

# 2. Restore state
tar xzf ~/openclaw-backup-YYYYMMDD.tgz -C ~/10claude/

# 3. Recreate .env (not in backup — store separately)
# Add ANTHROPIC_API_KEY, GIT identity, UI_USER, UI_PASS_HASH

# 4. Re-authenticate Claude CLI on the new machine
claude auth login

# 5. Start
bash scripts/setup_10claude.sh
```

### If containers fail to start

```bash
# Check logs
docker compose logs openclaw
docker compose logs runtime-daemon
docker compose logs caddy

# Rebuild images (e.g. after Dockerfile change)
docker compose build --no-cache
docker compose up -d
```

### Read task logs

All daemon output is in one file. Filter by task ID:

```bash
grep task-abc123 ~/10claude/logs/daemon.log
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| UI shows no tasks | `openclaw` container not running | `docker compose up -d openclaw` |
| Tasks stuck in Pending | `runtime-daemon` not running | `docker compose up -d runtime-daemon` |
| `claude` CLI fails inside daemon | Claude CLI not authenticated | `docker exec -it runtime-daemon claude auth login` |
| `ANTHROPIC_API_KEY not set` error | Missing from `.env` | Add key to `.env`, `docker compose up -d --force-recreate` |
| Caddy shows 502 Bad Gateway | `openclaw` not ready yet | Wait 15s for pip install on startup |
| Self-signed cert warning on phone | Expected on first visit | Accept/trust the cert once |
| Task failed — want to retry | Any failure | Click Retry in the UI or `POST /api/retry/{task_id}` |
| `UI_PASS_HASH` variable warnings | `$` not escaped in `.env` | Double every `$` → `$$` in the hash value |
