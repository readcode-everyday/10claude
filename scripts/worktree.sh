#!/bin/bash
# Manage git worktrees for parallel coding workers.
# Usage:
#   worktree.sh create <worker-id> <project-path>
#   worktree.sh remove <worker-id> <project-path>
#
# create: adds a worktree at /app/workspace/worktrees/<worker-id> on a new branch
#         prints the worktree path on stdout
# remove: removes the worktree and deletes the branch
set -euo pipefail

CMD=${1:?usage: worktree.sh create|remove <worker-id> <project-path>}
WORKER_ID=${2:?missing worker-id}
PROJECT=${3:?missing project-path}
WORKTREE_BASE=/app/workspace/worktrees
WORKTREE_PATH="$WORKTREE_BASE/$WORKER_ID"

case "$CMD" in
  create)
    mkdir -p "$WORKTREE_BASE"
    git -C "$PROJECT" worktree add "$WORKTREE_PATH" -b "worker/$WORKER_ID"
    echo "$WORKTREE_PATH"
    ;;
  remove)
    git -C "$PROJECT" worktree remove "$WORKTREE_PATH" --force 2>/dev/null || true
    git -C "$PROJECT" branch -D "worker/$WORKER_ID" 2>/dev/null || true
    ;;
  *)
    echo "Unknown command: $CMD. Use create or remove." >&2
    exit 1
    ;;
esac
