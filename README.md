# OpenClaw — Autonomous Dev Manager

## What It Is

OpenClaw is a **personal autonomous software engineering system**. You interact
with it as a dev manager: describe what you want done, and the system handles
planning and execution autonomously.

```
You  →  OpenClaw (control plane)
           │
           ├─ calls LLM to structure intent
           ├─ writes task to filesystem queue
           │
           ▼
     runtime-daemon (execution plane)
           │
           ├─ polls queue every 3 s
           ├─ calls LLM to decompose into sub-goals
           │     each sub-goal has a done_when success criterion
           │
           └─ for each sub-goal:  ◄─────────────────────┐
                 │                                        │
                 ├─ plan DAG of parallel tasks            │
                 ├─ workers execute concurrently          │
                 │     each worker → isolated git worktree│
                 │     code engine  → Claude CLI          │
                 ├─ evaluate done_when criterion          │
                 ├─ if major change → checkpoint + snapshot
                 └─ advance to next sub-goal ─────────────┘
           │
           ▼
     /workspace/results/{task_id}.json
```

---

## How It Works

### 1 — Submit a task

Send your intent to OpenClaw. It calls an LLM to clarify and structure the
request, then appends a task record to the **filesystem queue**:

```
/workspace/queue/tasks.jsonl
```

Each line is a JSON object:

```json
{"id": "abc123", "prompt": "Add OAuth2 login to the API"}
```

### 2 — Daemon picks up the task

`runtime-daemon` polls the queue every 3 seconds. For each new task it:

1. Calls the LLM to generate a **DAG of subtasks** (analyze → implement → test)
2. Hands the DAG to the parallel executor

### 3 — Hierarchical decomposition with success criteria

Before planning a DAG, `hierarchical_planner` asks the LLM to break the goal
into ordered sub-goals. Each sub-goal carries a `done_when` field — its success
criterion:

```json
[
  {"goal": "scaffold the data model", "done_when": "migrations exist and tests pass"},
  {"goal": "implement the API endpoint", "done_when": "endpoint returns 200 with correct schema"},
  {"goal": "write integration tests",   "done_when": "full test suite is green"}
]
```

This decomposition is the multi-turn loop: the system works through sub-goals
one at a time, evaluating each `done_when` criterion before advancing to the
next. A complex task can therefore span many back-and-forth planning and
execution cycles without human intervention.

### 4 — Workers execute in parallel

For each sub-goal the `dag_planner` produces a DAG of fine-grained nodes
(analyze → implement → test). The `parallel_executor` resolves dependencies and
runs independent nodes concurrently. Each worker:

- Gets its own isolated **git worktree** (no branch collisions)
- Invokes the **Claude CLI** as the code engine
- Retries on failure with exponential back-off

### 5 — Success criteria evaluated before continuing

After each sub-goal's DAG completes, `goal_evaluator` asks the LLM whether the
`done_when` criterion is satisfied given the execution evidence. If not, the
loop halts and records a failure — it does not blindly advance to the next
sub-goal.

### 6 — Checkpoint and snapshot before major changes

After every successful DAG execution, the system checks whether the change
qualifies as *major* (configurable in `config/major_change.yaml`):

| Trigger | Threshold |
|---------|-----------|
| Files changed | ≥ 5 |
| Lines inserted | ≥ 50 |
| Lines deleted | ≥ 20 |
| Always-major paths | `config/`, `architecture`, `database schema`, `migration` |

When triggered, **before** proceeding to the next sub-goal:

1. `git add -A && git commit` — code checkpoint
2. `tar czf /backups/snapshot-{timestamp}.tgz /workspace` — full workspace snapshot

This means no major state change is ever lost. Snapshots land in:

```
/backups/snapshot-{timestamp}.tgz
```

### 7 — Results are written

On full completion the result is written to:

```
/workspace/results/{task_id}.json
```

Successful runs also store experience in semantic memory so future planning
improves over time.

---

## Directory Structure

```
10claude/
├── openclaw/               # Agent runtime code
│   ├── adapters/
│   │   ├── coder/          # Claude CLI + Codex CLI adapters
│   │   ├── git/            # Git / worktree helpers
│   │   └── llm/            # LLM provider adapters (Anthropic, OpenAI, local)
│   └── tools/
│       ├── runtime_engine.py       # Daemon main loop
│       ├── dag_planner.py          # LLM → DAG
│       ├── parallel_executor.py    # Concurrent DAG runner
│       ├── coding_agent_router.py  # Dispatch to Claude/Codex
│       ├── llm_router.py           # Provider routing
│       ├── semantic_memory.py      # Experience store
│       ├── progress_memory.py      # Checkpoints & snapshots
│       └── status_ui.py            # FastAPI observability API
├── config/                 # Policies and routing config
│   ├── llm_backends.yaml   # LLM provider selection
│   ├── coding_agents.yaml  # Coding agent selection (claude / codex)
│   └── runtime_parallelism.yaml
├── workspace/              # ALL persistent runtime state
│   ├── queue/tasks.jsonl   # Filesystem task queue
│   ├── results/            # Per-task result JSON
│   └── semantic_memory/    # Long-term experience index
├── backups/                # Automatic snapshots before major changes
├── logs/                   # Persistent daemon logs
├── documentation/          # Architecture notes
├── scripts/                # Setup helpers
├── compose.yaml            # Docker orchestration
├── Dockerfile
└── .env                    # Secrets (API keys)
```

> **Persistence rule:** containers are disposable — only data written to a
> host-mounted path survives a restart. Mounted paths: `/workspace`, `/logs`,
> `/backups`, `/app/openclaw`, `/app/config`. Anything written elsewhere
> (e.g. `/tmp`) is lost on container restart.

---

## Containers

### `openclaw`

Control plane + observability UI. Receives user intent, enqueues tasks, reads
results. Runs the FastAPI server on port **18888**:

- `GET /tasks` — list completed task IDs
- `GET /results/{task_id}` — fetch result JSON

### `runtime-daemon`

Execution plane. Polls the queue, plans DAGs via LLM, runs coding agents in
parallel, writes results and memory. Mounts `docker.sock` for test container
execution — kept separate from the control plane for this reason.

---

## Configuration

| File | Purpose |
|------|---------|
| `config/llm_backends.yaml` | LLM provider (`openai` / `anthropic` / `local`) and model |
| `config/coding_agents.yaml` | Default coding agent (`claude` / `codex`) |
| `config/runtime_parallelism.yaml` | Worker pool size |
| `config/major_change.yaml` | Thresholds that trigger auto-checkpoint (file count, line count, always-major paths) |
| `config/architecture.yaml` | Protected paths the planner must not touch |
| `.env` | API keys |

---

## Setup

```bash
chmod +x scripts/setup_10claude.sh
./scripts/setup_10claude.sh
docker compose up -d
```

---

## Host ↔ Container Volume Mapping

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./workspace` | `/workspace` | All persistent runtime state |
| `./logs` | `/logs` | Persistent logs |
| `./openclaw` | `/app/openclaw` | Source code |
| `./config` | `/app/config` | Configuration |
| `./.env` | `/app/.env` | Secrets |
| `~/.ssh` | `/root/.ssh` | Git access |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Test container execution |

---

## Backup & Restore

```bash
# Backup everything that matters
tar czf backup.tgz workspace logs config .env

# Restore
tar xzf backup.tgz
```

This captures the full agent memory, task history, and configuration.

---

## Architecture Principle

**Containers are disposable. Filesystem state is permanent.**

OpenClaw is the control plane substrate. The autonomy engine (daemon + DAG
executor + parallel workers) runs on top of it. They are separate concerns,
separately containerised, communicating only through the filesystem queue.
