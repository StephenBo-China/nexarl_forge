#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ $# -gt 0 ]]; then
  export MEMORY_REVIEW_PROJECT_ROOT="$1"
elif [[ -z "${MEMORY_REVIEW_PROJECT_ROOT:-}" ]]; then
  unset MEMORY_REVIEW_PROJECT_ROOT
fi
python3 "${APP_ROOT}/scripts/memory_review_queue.py" ensure-server
