#!/usr/bin/env bash
set -euo pipefail

CANDIDATE_ROOT="${1:?candidate root required}"
TRUSTED_ROOT="${2:?trusted harness root required}"
STAGING_ROOT="${3:?staging root required}"

STAGED_PATH="$(
  python3 -I "${TRUSTED_ROOT}/scripts/ci/stage_candidate_source.py" \
    "${CANDIDATE_ROOT}" \
    "${STAGING_ROOT}" 2>/dev/stderr
)"
if [ -z "${STAGED_PATH}" ] || [ ! -d "${STAGED_PATH}" ]; then
  echo "stage_candidate_missing_output_path path=${STAGED_PATH:-empty}" >&2
  exit 1
fi
printf '%s\n' "${STAGED_PATH}"
