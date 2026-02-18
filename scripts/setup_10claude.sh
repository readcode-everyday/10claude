#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs/daemon logs/containers logs/tasks

docker compose up -d --build

echo "Started containers: openclaw, runtime-daemon"
echo "Daemon log:    logs/daemon/daemon.log"
echo "Task logs:     logs/tasks/<task-id>.log"
echo "Container logs: logs/containers/"
