#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if ! docker version >/dev/null 2>&1; then
  echo "Docker is not available. Start Docker and retry." >&2
  exit 1
fi
docker build -t bridle-node-agent:local -f "$REPO/docker/node-agent.Dockerfile" "$REPO"
docker build -t bridle-main-agent:local -f "$REPO/docker/main-agent.Dockerfile" "$REPO"
echo "Images built: bridle-node-agent:local, bridle-main-agent:local"
