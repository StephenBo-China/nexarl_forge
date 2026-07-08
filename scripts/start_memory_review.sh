#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="${1:-${MEMORY_REVIEW_PROJECT_ROOT:-$(pwd)}}"

export MEMORY_REVIEW_PROJECT_ROOT="${PROJECT_ROOT}"
python3 "${APP_ROOT}/scripts/memory_review_queue.py" ensure-server
