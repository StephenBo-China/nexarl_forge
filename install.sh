#!/usr/bin/env bash
set -euo pipefail
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${SOURCE_ROOT}/scripts/vibe_memory_cli.py" install --source-root "${SOURCE_ROOT}" "$@"
