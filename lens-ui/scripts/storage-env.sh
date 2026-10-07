#!/usr/bin/env bash
# Shared defaults for launchers; matches Python/Node storage path selection.
# Only LENS_* roots and XDG defaults select application-managed storage.
export LENS_DATA_DIR="${LENS_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/lens}"
export LENS_CACHE_DIR="${LENS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/lens}"
export LENS_LOG_DIR="${LENS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/lens/logs}"
export LENS_SCRATCH_DIR="${LENS_SCRATCH_DIR:-$LENS_CACHE_DIR/tmp}"
if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
  export LENS_RUNTIME_DIR="${LENS_RUNTIME_DIR:-$XDG_RUNTIME_DIR/lens}"
else
  export LENS_RUNTIME_DIR="${LENS_RUNTIME_DIR:-$LENS_SCRATCH_DIR/runtime}"
fi
