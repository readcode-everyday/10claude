#!/bin/bash
# Manage git worktrees for parallel coding workers.
# Usage:
#   worktree.sh create <worker-id> <project-path>
#   worktree.sh remove <worker-id> <project-path>
#
# create: adds a worktree at ~/10claude/workspace/worktrees/<worker-id> on a new
#         branch, prints the HOST path. Sub-agents see it at /app/workspace/worktrees/<worker-id>.
# remove: removes the worktree and deletes the branch.
#
# Call this from the dev agent's MAIN session (which runs on the host).
# Sub-agents run in sandbox containers where the worktree is at the container path.
set -euo pipefail

CMD=${1:?usage: worktree.sh create|remove <worker-id> <project-path>}
WORKER_ID=${2:?missing worker-id}
PROJECT=${3:?missing project-path}

# Host path — where the main dev session and git live
HOST_WORKTREE_BASE="$HOME/10claude/workspace/worktrees"
HOST_WORKTREE_PATH="$HOST_WORKTREE_BASE/$WORKER_ID"

# Container path — how sub-agents see the same directory
CONTAINER_WORKTREE_PATH="/app/workspace/worktrees/$WORKER_ID"

case "$CMD" in
  create)
    mkdir -p "$HOST_WORKTREE_BASE"
    git -C "$PROJECT" worktree add "$HOST_WORKTREE_PATH" -b "worker/$WORKER_ID"
    # Print BOTH paths so the main agent can pass the right one to sub-agents
    echo "host:$HOST_WORKTREE_PATH"
    echo "container:$CONTAINER_WORKTREE_PATH"
    ;;
  remove)
    git -C "$PROJECT" worktree remove "$HOST_WORKTREE_PATH" --force 2>/dev/null || true
    git -C "$PROJECT" branch -D "worker/$WORKER_ID" 2>/dev/null || true
    ;;
  *)
    echo "Unknown command: $CMD. Use create or remove." >&2
    exit 1
    ;;
esac
