# OpenClaw vs Autonomy Layer: Architecture Reminder

## Warning
This comparison reflects the system state at the time of writing.
If OpenClaw is updated, parts of this design analysis may no longer be true and should be re-validated.

## Executive Summary
OpenClaw already implements some of the layers we designed, but not all, and not at the same abstraction level.

- We did not fully duplicate OpenClaw.
- We did partially overlap in a few low-level areas.
- We built higher-level autonomy capabilities that OpenClaw does not natively provide.

## OpenClaw Native Scope (Current Reality)
OpenClaw is primarily:

- Tool-calling orchestration runtime
- Memory and session context
- Plugin system
- Session loop
- LLM routing
- Basic planning/execution

OpenClaw is not, by default:

- A full autonomous long-horizon software engineering agent
- DAG-parallel execution runtime
- Retry-graph orchestrator
- Persistent world-model cognition layer
- Checkpoint/snapshot reliability engine

## Layer-by-Layer Comparison

### 1) Task loop / orchestration
OpenClaw:

- Conversation loop
- Tool execution
- Memory per session

Our system:

- Daemon queue
- Persistent multi-task runtime
- Non-interactive autonomous execution

Conclusion:

- Different abstraction layer
- Not duplicate

### 2) LLM routing
OpenClaw:

- Multi-model support
- Config-driven routing
- Tool reasoning

We built:

- `llm_router`
- Role-based model selection
- Reasoning vs code separation

Conclusion:

- Partial overlap
- Candidate to reuse OpenClaw native routing instead of custom router
- First true duplication candidate

### 3) Tool / coding execution
OpenClaw:

- Plugin tool system
- Shell/code tool invocation

We built:

- `coding_agent_router`
- Claude/Codex adapters
- Retry + timeout policy

Conclusion:

- Mechanism overlap exists
- Policy layer is not duplicate
- OpenClaw = tool call substrate
- Ours = autonomous coding policy + retry graph
- Keep ours for higher-level autonomy

### 4) Memory
OpenClaw:

- Short-term memory
- Vector memory plugin
- Session recall

We built:

- Semantic experience store
- World model
- Failure history
- Long-horizon learning loop

Conclusion:

- Not duplicate
- Ours is long-term autonomous cognition layer

### 5) DAG planning
OpenClaw:

- No true DAG engine
- Mostly linear reasoning/tool loop

We built:

- Structured DAG schema
- Dependency graph
- Retry metadata
- Parallel-ready plans

Conclusion:

- Entirely new capability
- Not duplicate

### 6) Parallel execution
OpenClaw:

- Generally sequential
- Tool calls in turn

We built:

- Worker pool
- Dependency scheduling
- Concurrent node execution

Conclusion:

- Completely new
- Not duplicate

### 7) Retry / self-healing graph
OpenClaw:

- Limited tool-level retry
- Not graph-aware

We built:

- Exponential backoff
- Node-level retry
- DAG failure isolation

Conclusion:

- New capability
- Not duplicate

### 8) Hierarchical long-horizon autonomy
OpenClaw:

- No persistent multi-goal world model
- No goal decomposition control loop

We built:

- Hierarchical planner
- Goal evaluator
- World model
- Long-horizon execution loop

Conclusion:

- Beyond OpenClaw baseline
- Definitely not duplicate

### 9) Checkpointing / snapshots
OpenClaw:

- No automatic git checkpoint + filesystem snapshot loop

We built:

- Major-change detection
- Git checkpointing
- Workspace snapshots

Conclusion:

- New reliability layer
- Not duplicate

## Real Overlap vs New Value
What overlaps and can potentially be replaced by OpenClaw-native features:

- LLM routing
- Basic tool execution wrappers
- Short-term memory

What is new and strategically valuable:

- DAG execution
- Parallel worker pool
- Retry graph
- Hierarchical planning
- Evaluator loop
- World model
- Semantic long-term learning
- Checkpointing/snapshot reliability

These are the components that shift the system from a tool-calling agent to a true autonomous SWE runtime.

## Key Architectural Insight
- OpenClaw should be treated as substrate/control plane.
- Our implementation is an autonomy engine above that substrate.

Preferred integration direction:

- Do not rewrite OpenClaw.
- Run the long-horizon autonomy engine on top of OpenClaw's tool + memory primitives.

This minimizes duplication and preserves capability.

## Direct Answer (Recorded)
Did OpenClaw already have this code?

- Partially, at lower-level primitives.

Did we duplicate anything important?

- Small parts only (LLM router, tool call wrappers).
- No critical duplication at the autonomy layer.

Therefore, this work remains necessary if the target is:

- Autonomous long-running software engineer

and not merely:

- Chat tool with plugins

## Strategic Fork
Future direction must choose one:

1. Thin autonomy layer on top of OpenClaw (best engineering tradeoff)
2. Independent runtime replacing OpenClaw (more control, more maintenance)

This decision governs future system evolution.
