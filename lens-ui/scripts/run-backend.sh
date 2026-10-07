#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/storage-env.sh"
if [[ -x "${PYTHON_BIN:-}" ]]; then
  PYTHON="${PYTHON_BIN}"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
if ! "$PYTHON" -c 'import uvicorn' >/dev/null 2>&1; then
  echo "Backend dependency missing: uvicorn is not available in $PYTHON" >&2
  echo "Create a virtualenv and install the project requirements before starting Prism." >&2
  exit 1
fi
exec "$PYTHON" -m uvicorn llm_d_bench.api:app \
  --host "${BACKEND_HOST:-0.0.0.0}" \
  --port "${BACKEND_PORT:-8081}"
