#!/usr/bin/env bash
set -euo pipefail
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=""
for candidate in \
  "${VIBE_MEMORY_PYTHON:-}" \
  "$(command -v python3 2>/dev/null || true)" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /usr/bin/python3; do
  if [ -n "${candidate}" ] \
    && "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON="${candidate}"
    break
  fi
done

if [ -z "${PYTHON}" ]; then
  echo "vibe-memory install requires Python 3.10 or newer" >&2
  exit 1
fi

export VIBE_MEMORY_PYTHON="${PYTHON}"
"$PYTHON" "${SOURCE_ROOT}/scripts/vibe_memory_cli.py" install --source-root "${SOURCE_ROOT}" "$@"
"$HOME/.local/bin/vibe-memory" doctor --json
