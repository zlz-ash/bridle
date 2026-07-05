#!/usr/bin/env bash
set -euo pipefail

CANDIDATE_ROOT="${1:?candidate root required}"
TRUSTED_ROOT="${2:?trusted harness root required}"
SNAPSHOT="${3:?snapshot path required}"
MANIFEST="${TRUSTED_ROOT}/.github/trusted-docker-harness.txt"

exec python3 -I "${TRUSTED_ROOT}/scripts/ci/trusted_harness.py" \
  overlay \
  "$CANDIDATE_ROOT" \
  "$TRUSTED_ROOT" \
  "$MANIFEST" \
  "$SNAPSHOT"
