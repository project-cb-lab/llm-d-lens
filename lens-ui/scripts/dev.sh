#!/usr/bin/env bash
# Development service manager for llm-d-prism.
#
# Checks the environment and starts/stops the three local services:
#   - Vite frontend       (FRONTEND_PORT,   default 5173)
#   - Express API server  (SERVER_PORT,     default 3000)
#   - FastAPI backend     (BACKEND_PORT, default 8081)
#
# All ports are configurable via environment variables (see below), and the
# frontend/backend proxies read the same variables so everything stays in sync.
#
# Usage:
#   scripts/dev.sh check     # validate the environment without starting anything
#   scripts/dev.sh start     # start all services (background, logs in .dev-logs/)
#   scripts/dev.sh stop      # stop all services started by this script
#   scripts/dev.sh restart   # validate reuse map, then stop and start
#   scripts/dev.sh status    # show which services are running
#   scripts/dev.sh clean [-y|--yes]  # stop services, delete embedded DB + all
#                                    # local app data under ~/.llm-d-lens
#   scripts/dev.sh install-node  # install a supported Node.js into .dev-tools/
#
# When the system Node.js is missing or older than NODE_MIN_MAJOR, 'start'
# downloads an official Node.js build into .dev-tools/ (checksum-verified) and
# uses it for this script's services only. Set NODE_AUTO_INSTALL=0 to opt out.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Optional local secrets are intentionally ignored by Git.
if [[ -f "$ROOT_DIR/.secrets.env" ]]; then
    set -a
    source "$ROOT_DIR/.secrets.env"
    set +a
fi

# ---- Configuration (override via environment) -------------------------------
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
SERVER_PORT="${SERVER_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8081}"
VITE_HOST="${VITE_HOST:-0.0.0.0}"

# HTTPS is on by default: browsers only expose features like microphone
# access (getUserMedia) in a "secure context" (HTTPS or http://localhost), so
# a box reached over http://<lan-ip> needs HTTPS to unlock those features.
# A self-signed cert is generated automatically (idempotent -- reused across
# restarts) since there's usually no public DNS name for a Let's Encrypt cert
# on an internal box. Set ENABLE_HTTPS=0 to opt out and run plain HTTP.
ENABLE_HTTPS="${ENABLE_HTTPS:-1}"
TLS_DIR="${TLS_DIR:-$ROOT_DIR/.dev-tls}"
TLS_HOST="${TLS_HOST:-}"

# Minimum Node.js major version.
NODE_MIN_MAJOR="${NODE_MIN_MAJOR:-22}"

# Set NODE_AUTO_INSTALL=0 to disable downloading a local Node.js toolchain when
# the system one is missing or too old.
NODE_AUTO_INSTALL="${NODE_AUTO_INSTALL:-1}"

# Pin an exact version (e.g. v22.23.2) to skip the release-index lookup.
NODE_INSTALL_VERSION="${NODE_INSTALL_VERSION:-}"

# Used when nodejs.org/dist/index.json cannot be reached.
NODE_FALLBACK_VERSION="${NODE_FALLBACK_VERSION:-v22.23.2}"

NODE_DIST_URL="${NODE_DIST_URL:-https://nodejs.org/dist}"

# Node.js installed by this script lives here, never outside the repository.
TOOLS_DIR="$ROOT_DIR/.dev-tools"
LOCAL_NODE_DIR="$TOOLS_DIR/node"

# Minimum Rust toolchain version (warn + upgrade hint when lower).
RUST_MIN_VERSION="${RUST_MIN_VERSION:-1.80.0}"

# Shared persistent data, disposable cache and diagnostic log roots.
source "$ROOT_DIR/scripts/storage-env.sh"
EVALUATE_STORE="$LENS_DATA_DIR/metadata/evaluations"

# Optional IP address or DNS name advertised for deployment Model APIs. When
# empty, port-forwards remain localhost-only.
PRISM_MODEL_API_PUBLIC_HOST="${PRISM_MODEL_API_PUBLIC_HOST:-}"



# Optional comma-separated PCI allowlist restricting which XPUs/GPUs can be
# selected for deployment (read by the FastAPI backend and the Express server).
PRISM_GPU_PCI_ALLOWLIST="${PRISM_GPU_PCI_ALLOWLIST:-}"

# Shared secret for signed Node->Python internal calls (design section 8.3).
# The Express server signs internal reads with the authenticated caller's
# principal id; the FastAPI backend verifies the HMAC. Both processes must see
# the same value, so a local default keeps them in sync in development.
LENS_INTERNAL_AUTH_SECRET="${LENS_INTERNAL_AUTH_SECRET:-dev-internal-secret}"

# Deploy/Model Cache render Guide manifests from an llm-d checkout containing
# guides/. Default to a well-known location under $HOME so this works without
# manual setup; clone it there (or set LLM_D_ROOT) if it's missing.
LLM_D_ROOT="${LLM_D_ROOT:-$LENS_CACHE_DIR/llm-d}"
export LLM_D_ROOT

PID_DIR="$ROOT_DIR/.dev-pids"
LOG_DIR="$LENS_LOG_DIR/dev"

SERVICES=(frontend server backend)

# ---- Helpers -----------------------------------------------------------------
log()  { printf '\033[1;36m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[dev]\033[0m %s\n' "$*" >&2; }

# run_with_tail <window> <command...> — runs <command>, showing only the
# last <window> lines of its (often very verbose) combined stdout/stderr on
# screen, redrawn in place, instead of flooding the terminal with
# everything. On failure, the full output is dumped so the real error is
# still visible. Falls back to running the command directly when stdout
# isn't a terminal (e.g. CI logs), where the full output is actually useful.
run_with_tail() {
    local window="$1"
    shift

    if [[ ! -t 1 ]]; then
        "$@"
        return $?
    fi

    local log_file
    log_file="$(mktemp)"
    "$@" >"$log_file" 2>&1 &
    local cmd_pid=$! prev_lines=0 lines cols

    while kill -0 "$cmd_pid" 2>/dev/null; do
        [[ "$prev_lines" -gt 0 ]] && printf '\033[%dA\033[J' "$prev_lines"
        # Truncate to the terminal width (minus the 2-space indent) so every
        # displayed line occupies exactly one terminal row -- otherwise long
        # lines (pip's output is full of long URLs/filenames) wrap onto
        # multiple rows, wc -l undercounts them, and the next redraw's
        # cursor-up doesn't go up far enough, leaving stale lines behind and
        # making the output appear to endlessly scroll instead of staying
        # pinned to a 5-line window. Re-measured every iteration in case the
        # terminal is resized mid-install.
        cols="$(tput cols 2>/dev/null || echo 80)"
        lines="$(tail -n "$window" "$log_file" 2>/dev/null | cut -c "1-$((cols > 2 ? cols - 2 : cols))")"
        if [[ -n "$lines" ]]; then
            printf '%s\n' "$lines" | sed 's/^/  /'
            prev_lines="$(wc -l <<<"$lines")"
        else
            prev_lines=0
        fi
        sleep 0.2
    done
    [[ "$prev_lines" -gt 0 ]] && printf '\033[%dA\033[J' "$prev_lines"

    wait "$cmd_pid"
    local rc=$?
    if [[ "$rc" -ne 0 ]]; then
        fail "command failed (exit $rc) — full output:"
        cat "$log_file" >&2
    fi
    rm -f "$log_file"
    return "$rc"
}

pid_file() { printf '%s/%s.pid' "$PID_DIR" "$1"; }

is_running() {
    local pid
    pid="$(cat "$(pid_file "$1")" 2>/dev/null || echo '')"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

port_in_use() {
    local port="$1"
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"
}

# resolve_free_port <service> <requested-port> — return the requested port when
# it is free (or already owned by a running managed service), otherwise the next
# free port above it.
resolve_free_port() {
    local name="$1" port="$2"
    if is_running "$name"; then
        printf '%s\n' "$port"
        return 0
    fi
    while port_in_use "$port"; do
        port=$((port + 1))
    done
    printf '%s\n' "$port"
}

# ver_lt <v1> <v2> — returns 0 when v1 is lower than v2 (via sort -V).
ver_lt() {
    [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]] && [[ "$1" != "$2" ]]
}

# ---- Node.js toolchain -------------------------------------------------------
# node_major — major version of the node on PATH, or empty when unavailable.
node_major() {
    command -v node >/dev/null 2>&1 || return 0
    node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

node_is_supported() {
    local major
    major="$(node_major)"
    [[ -n "$major" ]] && [[ "$major" -ge "$NODE_MIN_MAJOR" ]]
}

# activate_local_node — put a previously downloaded toolchain on PATH. Never
# downloads anything, so it is safe to call from read-only commands.
activate_local_node() {
    [[ -x "$LOCAL_NODE_DIR/bin/node" ]] || return 1
    case ":$PATH:" in
        *":$LOCAL_NODE_DIR/bin:"*) ;;
        *) PATH="$LOCAL_NODE_DIR/bin:$PATH"; export PATH ;;
    esac
    return 0
}

# node_platform — Node.js release suffix for this machine, empty when the
# platform has no official prebuilt binary.
node_platform() {
    local os arch
    case "$(uname -s)" in
        Linux)  os="linux" ;;
        Darwin) os="darwin" ;;
        *)      return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64)  arch="x64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)             return 1 ;;
    esac
    printf '%s-%s\n' "$os" "$arch"
}

# resolve_node_version — newest release matching NODE_MIN_MAJOR, preferring LTS.
resolve_node_version() {
    if [[ -n "$NODE_INSTALL_VERSION" ]]; then
        printf '%s\n' "$NODE_INSTALL_VERSION"
        return 0
    fi
    local index
    index="$(curl --proto '=https' --tlsv1.2 -sSf -m 20 "$NODE_DIST_URL/index.json" 2>/dev/null || true)"
    if [[ -n "$index" ]] && command -v python3 >/dev/null 2>&1; then
        local resolved
        resolved="$(printf '%s' "$index" | python3 -c "
import json, sys
major = int(sys.argv[1])
try:
    releases = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
matching = [r for r in releases if r['version'].lstrip('v').split('.')[0].isdigit()
            and int(r['version'].lstrip('v').split('.')[0]) == major]
lts = [r for r in matching if r.get('lts')]
chosen = (lts or matching)
if not chosen:
    sys.exit(1)
print(chosen[0]['version'])
" "$NODE_MIN_MAJOR" 2>/dev/null || true)"
        if [[ -n "$resolved" ]]; then
            printf '%s\n' "$resolved"
            return 0
        fi
    fi
    printf '%s\n' "$NODE_FALLBACK_VERSION"
}

node_manual_hint() {
    warn "  Install Node.js >= $NODE_MIN_MAJOR.x manually, for example:"
    echo "    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash"
    echo '    export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"'
    echo "    nvm install $NODE_MIN_MAJOR && nvm use $NODE_MIN_MAJOR"
}

# install_node — download an official Node.js build into .dev-tools and put it
# on PATH. The tarball is verified against the signed SHASUMS256.txt listing.
install_node() {
    local platform version tarball url workdir
    if ! platform="$(node_platform)"; then
        fail "no official Node.js binary for $(uname -s)/$(uname -m)"
        node_manual_hint
        return 1
    fi
    for tool in curl tar; do
        command -v "$tool" >/dev/null 2>&1 || { fail "$tool is required to install Node.js"; return 1; }
    done
    command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || {
        fail "sha256sum or shasum is required to verify the Node.js download"
        return 1
    }

    version="$(resolve_node_version)"
    tarball="node-$version-$platform.tar.xz"
    url="$NODE_DIST_URL/$version/$tarball"
    log "installing Node.js $version into $LOCAL_NODE_DIR ..."

    mkdir -p "$TOOLS_DIR"
    workdir="$(mktemp -d "$TOOLS_DIR/node-download.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$workdir'" RETURN

    if ! curl --proto '=https' --tlsv1.2 -fL --progress-bar -m 600 -o "$workdir/$tarball" "$url"; then
        fail "failed to download $url"
        node_manual_hint
        return 1
    fi
    if ! curl --proto '=https' --tlsv1.2 -fL -sS -m 60 -o "$workdir/SHASUMS256.txt" "$NODE_DIST_URL/$version/SHASUMS256.txt"; then
        fail "failed to download the checksum list for $version"
        return 1
    fi

    local expected actual
    expected="$(awk -v file="$tarball" '$2 == file {print $1}' "$workdir/SHASUMS256.txt")"
    if [[ -z "$expected" ]]; then
        fail "$tarball is not listed in SHASUMS256.txt"
        return 1
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$workdir/$tarball" | awk '{print $1}')"
    else
        actual="$(shasum -a 256 "$workdir/$tarball" | awk '{print $1}')"
    fi
    if [[ "$expected" != "$actual" ]]; then
        fail "checksum mismatch for $tarball (expected $expected, got $actual)"
        return 1
    fi

    if ! tar -xJf "$workdir/$tarball" -C "$workdir"; then
        fail "failed to extract $tarball"
        return 1
    fi

    local target="$TOOLS_DIR/node-$version-$platform"
    rm -rf "$target"
    mv "$workdir/node-$version-$platform" "$target"
    ln -sfn "$target" "$LOCAL_NODE_DIR"

    activate_local_node || { fail "installed Node.js is not executable at $LOCAL_NODE_DIR/bin/node"; return 1; }
    log "Node.js ready: $(node --version) (npm $(npm --version 2>/dev/null || echo '?'))"
    warn "this toolchain is local to the repository; other shells still use the system Node.js"
    return 0
}

# ensure_node — guarantee a supported Node.js is on PATH for this script run.
ensure_node() {
    if node_is_supported; then
        return 0
    fi

    local previous
    previous="$(node_major)"

    if activate_local_node && node_is_supported; then
        log "using the repository-local Node.js $(node --version)"
    else
        if [[ "$NODE_AUTO_INSTALL" != "1" ]]; then
            fail "node $(node --version 2>/dev/null || echo 'MISSING') is older than the required $NODE_MIN_MAJOR.x (NODE_AUTO_INSTALL=0)"
            node_manual_hint
            return 1
        fi
        if [[ -n "$previous" ]]; then
            warn "node v$previous.x is too old (required >= $NODE_MIN_MAJOR.x) — installing a local toolchain"
        else
            warn "node is missing (required >= $NODE_MIN_MAJOR.x) — installing a local toolchain"
        fi
        install_node || return 1
    fi

    # Native addons are compiled against a specific Node ABI, so a major-version
    # switch invalidates whatever is already in node_modules.
    if [[ -n "$previous" && -d node_modules ]]; then
        warn "Node.js major changed v$previous -> $(node --version) — run 'npm install' if dependencies misbehave"
    fi
    return 0
}

# ---- node_modules setup -------------------------------------------------------
# ensure_node_modules — install npm dependencies when node_modules is missing
# OR stale relative to package-lock.json (so a `git pull` that adds/bumps a
# dependency, e.g. react-markdown, actually gets installed instead of being
# silently skipped just because a node_modules/ directory happens to exist).
ensure_node_modules() {
    local lockfile="package-lock.json"
    local stamp="node_modules/.install-stamp"
    local current_hash=""
    if [[ -f "$lockfile" ]]; then
        current_hash="$(sha256sum "$lockfile" 2>/dev/null | awk '{print $1}')"
    fi

    if [[ -d node_modules && -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" == "$current_hash" ]]; then
        return 0
    fi

    if [[ -d node_modules ]]; then
        log "package-lock.json changed since the last install — running 'npm install'..."
    else
        log "node_modules missing — running 'npm install'..."
    fi
    # --ignore-scripts: onnxruntime-node's postinstall tries to download a
    # native binary that's unreachable on some networks; harmless to skip
    # since the browser build (transformers.js) never uses it anyway.
    npm install --ignore-scripts || {
        fail "npm install failed — see output above"
        return 1
    }
    echo "$current_hash" > "$stamp"
}

# ---- venv setup ---------------------------------------------------------------
# ensure_venv — create the virtualenv and install Python deps when missing, or
# when pyproject.toml changed since the last install. The stamp mirrors
# ensure_node_modules: a `git pull` that adds/bumps a dependency (e.g. casbin)
# must not be silently skipped just because the venv exists and the probed
# imports (llm_d_bench/uvicorn/pgserver) still work.
ensure_venv() {
    local stamp=".venv/.install-stamp"
    local current_hash=""
    if [[ -f pyproject.toml ]]; then
        current_hash="$(sha256sum pyproject.toml 2>/dev/null | awk '{print $1}')"
    fi

    if [[ -x .venv/bin/python && -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" == "$current_hash" ]] \
        && .venv/bin/python -c "import llm_d_bench, uvicorn, pgserver" 2>/dev/null; then
        return 0
    fi

    if [[ ! -x .venv/bin/python ]]; then
        if ! command -v python3 >/dev/null 2>&1; then
            fail "python3 not found — cannot create the virtualenv"
            return 1
        fi
        log "creating virtualenv (.venv)..."
        python3 -m venv .venv || {
            fail "python3 -m venv failed — install python3-venv / ensurepip and retry"
            return 1
        }
    elif [[ -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" != "$current_hash" ]]; then
        log "pyproject.toml changed since the last install — refreshing Python dependencies..."
    fi

    # '[embedded-db]' pulls in pgserver, needed by ensure_database's default
    # built-in PostgreSQL fallback below. '[ldap]' pulls in ldap3 so the
    # Administration directory provider "Test" action works in local dev.
    log "installing Python dependencies (pip install -e .[embedded-db,ldap])..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
    run_with_tail 5 .venv/bin/python -m pip install -e ".[embedded-db,ldap]" || {
        fail "pip install -e .[embedded-db,ldap] failed — see output above"
        return 1
    }
    echo "$current_hash" > "$stamp"
}

# ---- database setup -------------------------------------------------------------
# ensure_database — auto-configure the built-in embedded PostgreSQL (no
# password, local dev only) the first time dev.sh runs with no database
# already configured (via env vars or a previous "Step 0" -- see
# llm_d_bench/db/system_router.py's get_database_status()). Unlike the
# installer (scripts/LensInstaller-Ubuntu-x86_64.sh), which always requires
# a password on its embedded database, dev.sh intentionally stays
# password-less for local-dev convenience.
ensure_database() {
    if ! .venv/bin/python -m llm_d_bench.db.bootstrap_cli --mode embedded; then
        fail "database auto-configuration failed — see output above"
        return 1
    fi
}

# ---- check -------------------------------------------------------------------
cmd_check() {
    local ok=1
    log "Checking environment..."

    # Pick up a toolchain a previous run installed, without downloading here.
    activate_local_node >/dev/null 2>&1 || true

    if command -v node >/dev/null 2>&1; then
        local node_major
        node_major="$(node --version | sed 's/^v//' | cut -d. -f1)"
        if [[ "$node_major" -ge "$NODE_MIN_MAJOR" ]]; then
            log "  node:     $(node --version)"
        else
            fail "  node:     $(node --version) (required >= $NODE_MIN_MAJOR.x)"
            warn "  Run 'scripts/dev.sh install-node' (or 'start') to install it locally."
            ok=0
        fi
    else
        fail "  node:     MISSING (required >= $NODE_MIN_MAJOR.x)"
        warn "  Run 'scripts/dev.sh install-node' (or 'start') to install it locally."
        ok=0
    fi
    command -v npm  >/dev/null 2>&1 && log "  npm:      $(npm --version)"  || { fail "  npm:      MISSING"; ok=0; }
    if command -v rustc >/dev/null 2>&1 && command -v cargo >/dev/null 2>&1; then
        local rust_ver
        rust_ver="$(rustc --version | awk '{print $2}')"
        if ver_lt "$rust_ver" "$RUST_MIN_VERSION"; then
            fail "  rust:     rustc $rust_ver (required >= $RUST_MIN_VERSION)"
            ok=0
            warn "  Upgrade Rust:"
            echo "    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
            echo '    source "$HOME/.cargo/env"'
            echo '    rustup default stable'
            echo '    rustc --version'
        else
            log "  rust:     rustc $rust_ver"
        fi
        log "  cargo:    $(cargo --version)"
    else
        fail "  rust:     rustc/cargo missing"
        ok=0
        warn "  Install Rust:"
        echo "    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
        echo '    source "$HOME/.cargo/env"'
        echo '    rustup default stable'
        echo '    rustc --version'
    fi
    command -v ss   >/dev/null 2>&1 || warn "  ss:       missing (port checks skipped)"

    [[ -d node_modules ]] && log "  node_modules: present" || { fail "  node_modules: missing — run 'npm install'"; ok=0; }

    if [[ -x .venv/bin/python ]]; then
        log "  venv:     $(.venv/bin/python --version 2>&1)"
        .venv/bin/python -c "import llm_d_bench, uvicorn" 2>/dev/null \
            && log "  python deps: llm_d_bench + uvicorn importable" \
            || { fail "  python deps: import failed — run 'pip install -e .'"; ok=0; }
    else
        fail "  venv:     missing (.venv/bin/python) — 'scripts/dev.sh start' will create it"
        ok=0
    fi

    if [[ -d "$EVALUATE_STORE" ]]; then
        log "  evaluate store: $EVALUATE_STORE"
    else
        warn "  evaluate store: $EVALUATE_STORE does not exist (will be created at runtime)"
    fi

    if [[ -d "$LLM_D_ROOT/guides" ]]; then
        log "  LLM_D_ROOT: $LLM_D_ROOT"
    else
        warn "  LLM_D_ROOT: $LLM_D_ROOT (missing guides/ — clone https://github.com/llm-d/llm-d there, or set LLM_D_ROOT, before using Deploy/Guide features)"
    fi

    log "  configured ports:"
    for svc in "${SERVICES[@]}"; do
        local port=""
        case "$svc" in
            frontend)   port="$FRONTEND_PORT";;
            server)     port="$SERVER_PORT";;
            backend)    port="$BACKEND_PORT";;
        esac
        if port_in_use "$port"; then
            warn "    $svc -> $port (IN USE)"
        else
            log "    $svc -> $port (free)"
        fi
    done

    if [[ "$ok" -ne 1 ]]; then
        fail "Environment check failed — resolve the issues above and re-run."
        exit 1
    fi
    log "Environment OK."
}

# ---- start -------------------------------------------------------------------

# Ensures a self-signed TLS cert exists (generating one on first run, reusing
# it after) and exports TLS_CERT_FILE/TLS_KEY_FILE for the server + frontend
# to pick up. No-op when ENABLE_HTTPS=0.
ensure_tls() {
    [[ "$ENABLE_HTTPS" == "1" ]] || return 0
    local host="$TLS_HOST"
    if [[ -z "$host" ]]; then
        host="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [[ -z "$host" ]]; then
        warn "could not detect a LAN IP for the TLS certificate; falling back to plain HTTP."
        warn "set TLS_HOST=<your-ip> to enable HTTPS, or ENABLE_HTTPS=0 to silence this."
        ENABLE_HTTPS=0
        return 0
    fi
    local cert_env
    if ! cert_env="$("$ROOT_DIR/scripts/generate-self-signed-cert.sh" "$host" "$TLS_DIR")"; then
        warn "failed to generate a self-signed TLS certificate; falling back to plain HTTP."
        ENABLE_HTTPS=0
        return 0
    fi
    eval "$cert_env"
    export TLS_CERT_FILE TLS_KEY_FILE
}

start_one() {
    local name="$1"
    shift
    if is_running "$name"; then
        warn "$name already running (pid $(cat "$(pid_file "$name")"))"
        return 0
    fi
    mkdir -p "$PID_DIR" "$LOG_DIR"
    log "starting $name..."
    local run_log="$name-$(date -u +%Y%m%dT%H%M%S%NZ)-$$.log"
    ln -sfn "$run_log" "$LOG_DIR/$name.log"
    nohup "$@" > "$LOG_DIR/$run_log" 2>&1 &
    echo "$!" > "$(pid_file "$name")"
}

wait_for_http() {
    local name="$1" url="$2" attempts="${3:-30}"
    local i=0
    while [[ $i -lt $attempts ]]; do
        if curl -sf -k -m 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    warn "$name did not become ready at $url — see $LOG_DIR/$name.log"
    return 1
}

# ---- MCP tool catalog ---------------------------------------------------------
# regenerate_mcp_tools — regenerate server/mcp/tools.ts from the FastAPI
# backend's live OpenAPI schema before every start, so the MCP tool catalog
# can never silently drift from the routes actually exposed by the backend.
# Non-fatal: falls back to whatever tools.ts is already on disk if this fails.
regenerate_mcp_tools() {
    if [[ ! -x .venv/bin/python || ! -f scripts/generate-mcp-tools.mjs ]]; then
        return 0
    fi
    mkdir -p "$LOG_DIR"
    log "regenerating MCP tool catalog from the backend API..."
    if ! node scripts/generate-mcp-tools.mjs >"$LOG_DIR/generate-mcp-tools.log" 2>&1; then
        warn "MCP tool catalog regeneration failed — see $LOG_DIR/generate-mcp-tools.log (using existing server/mcp/tools.ts)"
    fi
}

# Keep the reuse preflight ahead of cmd_stop on restart: a broken registration
# must not take down the existing development session. Pending refactor decisions
# remain blocking for the development check/CI, but not for local service startup.
prepare_start() {
    ensure_node || exit 1
    ensure_node_modules || exit 1
    ensure_venv || exit 1
    log "updating reusable capability map..."
    local reuse_status=0
    node tools/reuse/cli.mjs map || reuse_status=$?
    if [[ "$reuse_status" -ne 0 ]]; then
        fail "Reuse map update failed. Ask the coding AI to fix .reuse/catalog.json; services were not stopped."
        return "$reuse_status"
    fi
    log "validating reuse registrations and pending decisions..."
    node tools/reuse/cli.mjs check --purpose startup --base HEAD --report .cache/reuse/startup-report.json || reuse_status=$?
    if [[ "$reuse_status" -eq 2 ]]; then
        warn "Pending refactor decisions require review; continuing local startup. Details: .cache/reuse/startup-report.json"
        return 0
    fi
    if [[ "$reuse_status" -ne 0 ]]; then
        fail "Reuse check blocked startup (exit $reuse_status). Review the details above; services were not stopped."
        return "$reuse_status"
    fi
}

# Point operators to the protected first-run credential file without printing
# its contents. The file is removed once the password is changed.
print_initial_admin_credentials() {
    local cred_file="$LENS_DATA_DIR/credentials/initial_admin.txt"
    [[ -f "$cred_file" ]] || return 0
    log "administrator credentials are stored in the mode-600 file $cred_file; removed after the password is changed"
}

cmd_start() {
    prepare_start || return $?
    start_services
}

cmd_restart() {
    prepare_start || return $?
    cmd_stop
    start_services
}

start_services() {
    ensure_database || exit 1
    ensure_tls
    regenerate_mcp_tools

    # Remember what was requested so we can report any port that got shifted.
    local requested_frontend="$FRONTEND_PORT" requested_server="$SERVER_PORT" requested_backend="$BACKEND_PORT"

    # Shift off any port already occupied by another process.
    BACKEND_PORT="$(resolve_free_port backend "$BACKEND_PORT")"
    SERVER_PORT="$(resolve_free_port server "$SERVER_PORT")"
    FRONTEND_PORT="$(resolve_free_port frontend "$FRONTEND_PORT")"

    cmd_check

    local scheme="http"
    [[ "$ENABLE_HTTPS" == "1" ]] && scheme="https"

    start_one backend env \
        PRISM_MODEL_API_PUBLIC_HOST="$PRISM_MODEL_API_PUBLIC_HOST" \
        PRISM_GPU_PCI_ALLOWLIST="$PRISM_GPU_PCI_ALLOWLIST" \
        PRISM_MCP_URL="$scheme://127.0.0.1:$SERVER_PORT/api/mcp" \
        PRISM_MCP_CA_FILE="${TLS_CERT_FILE:-}" \
        LENS_INTERNAL_AUTH_SECRET="$LENS_INTERNAL_AUTH_SECRET" \
        .venv/bin/python -m uvicorn llm_d_bench.api:app \
        --host "${BACKEND_HOST:-0.0.0.0}" --port "$BACKEND_PORT"

    start_one server env SERVER_PORT="$SERVER_PORT" PORT="$SERVER_PORT" \
        PRISM_GPU_PCI_ALLOWLIST="$PRISM_GPU_PCI_ALLOWLIST" \
        LENS_INTERNAL_AUTH_SECRET="$LENS_INTERNAL_AUTH_SECRET" \
        SIMULATION_API_URL="http://127.0.0.1:$BACKEND_PORT" \
        TLS_CERT_FILE="${TLS_CERT_FILE:-}" TLS_KEY_FILE="${TLS_KEY_FILE:-}" \
        ./node_modules/.bin/tsx server/server.js

    start_one frontend env VITE_HOST="$VITE_HOST" VITE_PORT="$FRONTEND_PORT" \
        VITE_AUTH_PROXY_TARGET="$scheme://127.0.0.1:$SERVER_PORT" \
        VITE_API_PROXY_TARGET="$scheme://127.0.0.1:$SERVER_PORT" \
        TLS_CERT_FILE="${TLS_CERT_FILE:-}" TLS_KEY_FILE="${TLS_KEY_FILE:-}" \
        ./node_modules/.bin/vite

    log "waiting for services to become ready..."
    wait_for_http backend "http://127.0.0.1:$BACKEND_PORT/api/health"
    wait_for_http server     "$scheme://127.0.0.1:$SERVER_PORT/api/config"
    wait_for_http frontend   "$scheme://127.0.0.1:$FRONTEND_PORT/"

    log "services started:"
    log "  frontend   $scheme://localhost:$FRONTEND_PORT (bound to $VITE_HOST)"
    log "  server     $scheme://127.0.0.1:$SERVER_PORT"
    log "  backend    http://127.0.0.1:$BACKEND_PORT"
    if [[ "$ENABLE_HTTPS" == "1" ]]; then
        log "  HTTPS is on with a self-signed cert (browser will warn once; accept it) — needed for mic access etc."
    fi

    if [[ "$FRONTEND_PORT" != "$requested_frontend" || "$SERVER_PORT" != "$requested_server" || "$BACKEND_PORT" != "$requested_backend" ]]; then
        warn "requested ports were occupied; using free ports instead:"
        warn "  frontend  $requested_frontend -> $FRONTEND_PORT"
        warn "  server    $requested_server -> $SERVER_PORT"
        warn "  backend   $requested_backend -> $BACKEND_PORT"
    fi

    print_initial_admin_credentials
}

# ---- stop --------------------------------------------------------------------
cmd_stop() {
    local stopped=0
    for svc in "${SERVICES[@]}"; do
        local pf
        pf="$(pid_file "$svc")"
        if [[ -f "$pf" ]]; then
            local pid
            pid="$(cat "$pf")"
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                log "stopping $svc (pid $pid)..."
                kill "$pid" 2>/dev/null || true
                stopped=1
            fi
            rm -f "$pf"
        fi
    done
    [[ "$stopped" -eq 1 ]] && sleep 1
    log "all services stopped."
}

# ---- clean -------------------------------------------------------------------
# Best-effort graceful stop of a leftover embedded PostgreSQL process for
# <data_dir>. dev.sh's backend runs pgserver in-process, so killing the
# uvicorn pid in cmd_stop does not guarantee pgserver's own postgres
# subprocess exits with it -- read its postmaster.pid and signal it directly
# so `clean` doesn't rm -rf a data directory out from under a still-running
# server.
stop_embedded_postgres_process() {
    local data_dir="$1" pid_file pid waited=0
    pid_file="$data_dir/postmaster.pid"
    [[ -f "$pid_file" ]] || return 0
    pid="$(head -n1 "$pid_file" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 0
    log "stopping leftover embedded PostgreSQL (pid $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 20 ]]; do
        sleep 0.5
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "embedded PostgreSQL (pid $pid) did not stop gracefully — killing it."
        kill -KILL "$pid" 2>/dev/null || true
    fi
}

cmd_clean() {
    local yes=0
    [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && yes=1

    local db_dir bootstrap_file lens_home
    db_dir="$(python3 -c "import os; print(os.path.expanduser(os.environ.get('LLM_D_BENCH_EMBEDDED_DB_DIR', '~/.llm-d-lens/db/data')))" 2>/dev/null || printf '%s/.llm-d-lens/db/data' "$HOME")"
    bootstrap_file="$(python3 -c "import os; print(os.path.expanduser(os.environ.get('LLM_D_BENCH_DB_BOOTSTRAP_FILE', '~/.llm-d-lens/db-bootstrap.json')))" 2>/dev/null || printf '%s/.llm-d-lens/db-bootstrap.json' "$HOME")"
    lens_home="$HOME/.llm-d-lens"

    warn "this will stop dev services and permanently delete:"
    warn "  - the embedded database: $db_dir"
    warn "  - all local app data:    $lens_home (evaluate results, deploy manifests, datasets, cluster cache, etc.)"
    warn "  - dev logs/pids:         $PID_DIR, $LOG_DIR"
    if [[ "$yes" -ne 1 ]]; then
        local reply
        read -r -p "Type 'yes' to continue: " reply || true
        if [[ "$reply" != "yes" ]]; then
            log "aborted -- nothing was deleted."
            exit 1
        fi
    fi

    cmd_stop
    stop_embedded_postgres_process "$db_dir"

    rm -rf "$lens_home"
    # lens_home already covers db_dir/bootstrap_file by default, but an
    # override (LLM_D_BENCH_EMBEDDED_DB_DIR / _DB_BOOTSTRAP_FILE) could point
    # elsewhere -- remove those explicitly too so `clean` is correct either way.
    rm -rf "$db_dir"
    rm -f "$bootstrap_file"
    rm -rf "$PID_DIR" "$LOG_DIR"

    log "clean complete -- local database and app data have been removed."
}

# ---- status ------------------------------------------------------------------
cmd_status() {
    for svc in "${SERVICES[@]}"; do
        if is_running "$svc"; then
            log "  $svc: running (pid $(cat "$(pid_file "$svc")"))"
        else
            log "  $svc: stopped"
        fi
    done
}

# ---- dispatch ----------------------------------------------------------------
dev_main() {
case "${1:-}" in
    check)       cmd_check ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    status)      cmd_status ;;
    clean)       cmd_clean "${2:-}" ;;
    install-node) ensure_node ;;
    *)
        echo "Usage: $0 {check|start|stop|restart|status|clean|install-node}"
        echo
        echo "Ports (configurable via environment):"
        echo "  FRONTEND_PORT   (default 5173)  Vite frontend"
        echo "  SERVER_PORT     (default 3000)  Express API server"
        echo "  BACKEND_PORT    (default 8081)  FastAPI backend"
        echo
        echo "HTTPS (on by default, self-signed cert, needed for mic access etc. off localhost):"
        echo "  ENABLE_HTTPS    (default 1)     set to 0 to run plain HTTP instead"
        echo "  TLS_HOST        (default auto-detected LAN IP) host/IP the cert is issued for"
        echo "  TLS_DIR         (default .dev-tls) where the generated cert/key are cached"
        echo
        echo "Node.js toolchain:"
        echo "  NODE_MIN_MAJOR        (default 22)   minimum supported major version"
        echo "  NODE_AUTO_INSTALL     (default 1)    set to 0 to never download Node.js"
        echo "  NODE_INSTALL_VERSION  (default auto) pin an exact version, e.g. v22.23.2"
        echo
        echo "clean [-y|--yes]  stop services and delete the embedded database + all"
        echo "                  local app data under ~/.llm-d-lens (asks for confirmation"
        echo "                  unless -y/--yes is given)"
        exit 1
        ;;
esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    dev_main "$@"
fi
