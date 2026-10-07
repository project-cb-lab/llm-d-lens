#!/usr/bin/env bash
# LensInstaller-Ubuntu-x86_64.sh — llm-d Lens installer for Ubuntu (x86_64).
#
# Installs llm-d Lens (frontend + API server + FastAPI backend) as a
# standalone, production-ready deployment on Ubuntu 22.04 LTS or newer.
#
# Modes:
#   Silent mode        : ./LensInstaller-Ubuntu-x86_64.sh --silent [flags]
#                         No prompts. Uses flags/env vars or sane defaults.
#                         Suitable for scripted/CI installs (mirrors dev.sh).
#   Interactive mode    : ./LensInstaller-Ubuntu-x86_64.sh   (default)
#                         Asks for the install path and ports, each with a
#                         sensible default the user can just press Enter to
#                         accept.
#   Uninstall           : ./LensInstaller-Ubuntu-x86_64.sh --uninstall [--install-dir DIR]
#                         Stops services and removes the installation. Prompts
#                         whether to keep data (private/run_store and the port
#                         config); data is KEPT by default. Use --purge to wipe
#                         everything non-interactively, or --keep-data to keep
#                         it non-interactively.
#
# Usage:
#   scripts/LensInstaller-Ubuntu-x86_64.sh [--silent|-y] [options]
#
# Options:
#   --silent, -y            Non-interactive install using defaults/flags.
#   --install-dir DIR       Where to install (default: $HOME/llm-d-lens).
#   --app-port PORT         Port for the web app/API (default: 3000).
#   --backend-port PORT     Port for the internal FastAPI backend (default: 8081).
#   --no-https              Serve plain HTTP instead of auto-HTTPS (self-signed cert).
#   --tls-host HOST         Host/IP the auto-generated HTTPS cert is issued for
#                           (default: this machine's detected LAN IP).
#   --no-start              Do not start services after installing.
#   --no-apt                Never use apt/sudo to install missing system packages.
#   --db-mode MODE          Database mode: embedded (default) or external. Silent
#                           installs always use embedded unless this is set.
#   --db-engine ENGINE      External database engine: postgresql (default), mysql,
#                           oracle, or mssql. Only used with --db-mode external.
#   --db-host HOST          External database host (required for --db-mode external).
#   --db-port PORT          External database port (default: the engine's standard port).
#   --db-name NAME          External database name (required for --db-mode external).
#   --db-user USER          External database username.
#   --db-password PASSWORD  External database password.
#   --db-embedded-password PASSWORD  Password to enforce on the built-in embedded
#                           PostgreSQL. Auto-generated if omitted (silent installs)
#                           or prompted for (interactive installs). Only used with
#                           --db-mode embedded.
#   --uninstall             Uninstall llm-d Lens from --install-dir.
#   --keep-data             With --uninstall: keep data, skip the prompt.
#   --purge                 With --uninstall: remove data too, skip the prompt.
#   --keep-db               With --uninstall: keep the embedded database, skip the prompt.
#   --purge-db              With --uninstall: delete the embedded database, skip the prompt.
#                           Only applies when the install used --db-mode embedded; the
#                           embedded database's own process is always stopped either way.
#   -h, --help              Show this help and exit.
set -euo pipefail

# ---- Paths -------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Colors & styling (Copilot-CLI-esque) -------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    BLUE=$'\033[34m'; MAGENTA=$'\033[35m'; CYAN=$'\033[36m'; WHITE=$'\033[37m'
    # "llm-" is a muted grey, "d" is a deep plum/purple — the brand mark.
    LOGO_GRAY=$'\033[38;5;245m'; LOGO_PLUM=$'\033[38;5;97m'
else
    BOLD=""; DIM=""; RESET=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; MAGENTA=""; CYAN=""; WHITE=""
    LOGO_GRAY=""; LOGO_PLUM=""
fi

info()  { printf '%s[lens]%s %s\n' "$CYAN$BOLD" "$RESET" "$*"; }
ok()    { printf '%s[lens]%s %s✔%s %s\n' "$CYAN$BOLD" "$RESET" "$GREEN" "$RESET" "$*"; }
warn()  { printf '%s[lens]%s %s⚠%s %s\n' "$YELLOW$BOLD" "$RESET" "$YELLOW" "$RESET" "$*" >&2; }
fail()  { printf '%s[lens]%s %s✖%s %s\n' "$RED$BOLD" "$RESET" "$RED" "$RESET" "$*" >&2; }
step()  { printf '\n%s➜ %s%s\n' "$MAGENTA$BOLD" "$*" "$RESET"; }

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
            printf '%s\n' "$lines" | sed "s/^/  ${DIM}/;s/\$/${RESET}/"
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

print_logo() {
    [[ "${QUIET_LOGO:-0}" == "1" ]] && return 0
    local logo=(
        ' _ _                     _       _                '
        '| | |_ __ ___         __| |     | | ___ _ __  ___ '
        "| | | '_ \` _ \\ _____ / _\` |_____| |/ _ \\ '_ \\/ __|"
        '| | | | | | | |_____| (_| |_____| |  __/ | | \__ \'
        '|_|_|_| |_| |_|      \__,_|     |_|\___|_| |_|___/'
    )
    echo
    # Column 21 is where "llm-" ends and "-lens" begins in the rendered glyphs.
    for line in "${logo[@]}"; do
        printf '  %s%s%s%s%s%s\n' \
            "$LOGO_GRAY$BOLD" "${line:0:21}" "$RESET" \
            "$LOGO_PLUM$BOLD" "${line:21}" "$RESET"
    done
    # A plain, understated bar beneath the wordmark — matching its two tones.
    printf '  %s▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇%s%s▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇%s\n' "$LOGO_GRAY" "$RESET" "$LOGO_PLUM" "$RESET"
    printf '  %sLLM-D-LENS%s %sInstaller for Ubuntu (x86_64)%s\n\n' "$BOLD$WHITE" "$RESET" "$DIM" "$RESET"
}

# ---- Defaults / configuration -------------------------------------------------
SILENT=0
INSTALL_DIR_DEFAULT="$HOME/llm-d-lens"
INSTALL_DIR="${INSTALL_DIR:-$INSTALL_DIR_DEFAULT}"
APP_PORT="${APP_PORT:-${SERVER_PORT:-3000}}"
BACKEND_PORT="${BACKEND_PORT:-8081}"
START_AFTER_INSTALL=1
ALLOW_APT=1
UNINSTALL=0
UNINSTALL_ARGS=()
# HTTPS on by default: browsers only expose mic access etc. in a "secure
# context" (HTTPS or http://localhost), so a bare-metal box reached over
# http://<lan-ip> needs it. A self-signed cert is generated automatically.
ENABLE_HTTPS=1
TLS_HOST="${TLS_HOST:-}"

# Application-level ("Step 0") database configuration -- see
# llm_d_bench/db/bootstrap_cli.py. Silent installs always default to the
# built-in embedded PostgreSQL unless --db-mode/--db-* flags override it;
# interactive installs are asked via db_interactive_configure() below.
DB_MODE="${DB_MODE:-embedded}"
DB_ENGINE="${DB_ENGINE:-postgresql}"
DB_HOST="${DB_HOST:-}"
DB_PORT="${DB_PORT:-}"
DB_NAME="${DB_NAME:-}"
DB_USER="${DB_USER:-}"
DB_PASSWORD="${DB_PASSWORD:-}"
# Password enforced on the built-in embedded PostgreSQL's superuser (only
# used when DB_MODE=embedded; see llm_d_bench/db/settings.py's
# _harden_embedded_postgres_password). Unlike scripts/dev.sh's local-dev
# embedded database (intentionally password-less), the installer always
# requires one -- prompted interactively, or auto-generated for silent
# installs if not supplied via --db-embedded-password.
DB_EMBEDDED_PASSWORD="${DB_EMBEDDED_PASSWORD:-}"
# Set to 1 by configure_database() when DB_EMBEDDED_PASSWORD above was
# auto-generated (not supplied by the user) -- the final "Done" summary uses
# this to decide whether the password needs to be shown/reminded about, so
# it isn't only buried in the mid-install log output.
DB_EMBEDDED_PASSWORD_AUTOGEN=0

NODE_MIN_MAJOR="${NODE_MIN_MAJOR:-22}"
NODE_FALLBACK_VERSION="${NODE_FALLBACK_VERSION:-v22.23.2}"
NODE_DIST_URL="${NODE_DIST_URL:-https://nodejs.org/dist}"
UBUNTU_MIN_VERSION="22.04"

print_help() {
    print_logo
    sed -n '2,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- Argument parsing ----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --silent|-y)       SILENT=1; shift ;;
        --install-dir)     INSTALL_DIR="$2"; shift 2 ;;
        --install-dir=*)   INSTALL_DIR="${1#*=}"; shift ;;
        --app-port)        APP_PORT="$2"; shift 2 ;;
        --app-port=*)      APP_PORT="${1#*=}"; shift ;;
        --backend-port)    BACKEND_PORT="$2"; shift 2 ;;
        --backend-port=*)  BACKEND_PORT="${1#*=}"; shift ;;
        --no-https)        ENABLE_HTTPS=0; shift ;;
        --tls-host)        TLS_HOST="$2"; shift 2 ;;
        --tls-host=*)      TLS_HOST="${1#*=}"; shift ;;
        --no-start)         START_AFTER_INSTALL=0; shift ;;
        --no-apt)           ALLOW_APT=0; shift ;;
        --db-mode)          DB_MODE="$2"; shift 2 ;;
        --db-mode=*)        DB_MODE="${1#*=}"; shift ;;
        --db-engine)        DB_ENGINE="$2"; shift 2 ;;
        --db-engine=*)      DB_ENGINE="${1#*=}"; shift ;;
        --db-host)          DB_HOST="$2"; shift 2 ;;
        --db-host=*)        DB_HOST="${1#*=}"; shift ;;
        --db-port)          DB_PORT="$2"; shift 2 ;;
        --db-port=*)        DB_PORT="${1#*=}"; shift ;;
        --db-name)          DB_NAME="$2"; shift 2 ;;
        --db-name=*)        DB_NAME="${1#*=}"; shift ;;
        --db-user)          DB_USER="$2"; shift 2 ;;
        --db-user=*)        DB_USER="${1#*=}"; shift ;;
        --db-password)      DB_PASSWORD="$2"; shift 2 ;;
        --db-password=*)    DB_PASSWORD="${1#*=}"; shift ;;
        --db-embedded-password)    DB_EMBEDDED_PASSWORD="$2"; shift 2 ;;
        --db-embedded-password=*)  DB_EMBEDDED_PASSWORD="${1#*=}"; shift ;;
        --uninstall)        UNINSTALL=1; shift ;;
        --keep-data)        UNINSTALL_ARGS+=("--keep-data"); shift ;;
        --purge)            UNINSTALL_ARGS+=("--purge"); shift ;;
        --keep-db)          UNINSTALL_ARGS+=("--keep-db"); shift ;;
        --purge-db)         UNINSTALL_ARGS+=("--purge-db"); shift ;;
        -h|--help)          print_help; exit 0 ;;
        *) fail "unknown option: $1"; print_help; exit 1 ;;
    esac
done

# ---- Environment gates ---------------------------------------------------------
require_ubuntu() {
    if [[ ! -r /etc/os-release ]]; then
        fail "cannot detect the OS (/etc/os-release missing) — this installer supports Ubuntu only."
        exit 1
    fi
    # shellcheck disable=SC1091
    source /etc/os-release
    local id="${ID:-}" id_like="${ID_LIKE:-}" version="${VERSION_ID:-0}"
    if [[ "$id" != "ubuntu" && "$id_like" != *ubuntu* ]]; then
        fail "unsupported distribution: ${PRETTY_NAME:-$id} — this installer supports Ubuntu $UBUNTU_MIN_VERSION+."
        exit 1
    fi
    if [[ "$(printf '%s\n%s\n' "$version" "$UBUNTU_MIN_VERSION" | sort -V | head -n1)" != "$UBUNTU_MIN_VERSION" ]]; then
        fail "Ubuntu $version detected — this installer requires Ubuntu $UBUNTU_MIN_VERSION or newer."
        exit 1
    fi
    ok "Ubuntu $version detected (${PRETTY_NAME:-})"
}

require_arch() {
    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "x86_64" ]]; then
        fail "architecture $arch detected — this installer is built for x86_64 only."
        exit 1
    fi
    ok "architecture x86_64 detected"
}

# ---- Port conflict handling -------------------------------------------------------
# This host may be shared with other users/instances, so a chosen port
# (default or explicitly requested) can already be bound by someone else.
port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -H -ltn "( sport = :$port )" 2>/dev/null | grep -q .
        return
    fi
    # ss not installed -- fall back to a raw connect probe.
    (: < "/dev/tcp/127.0.0.1/$port") 2>/dev/null
}

# find_free_port <start> -> echoes the first free port at or after <start>.
find_free_port() {
    local port="$1" tries=0
    while port_in_use "$port"; do
        port=$((port + 1))
        tries=$((tries + 1))
        if [[ $tries -ge 200 ]]; then
            fail "could not find a free port at or after $1 (checked 200 ports)"
            exit 1
        fi
    done
    printf '%s\n' "$port"
}

# describe_port_owner <port> -> best-effort "who's holding this port" string.
# On this kind of shared host, the occupant is often another user's process,
# whose PID/command `ss` won't reveal without root — say so plainly instead
# of pretending we know, so the user isn't left guessing why a pick failed.
describe_port_owner() {
    local port="$1" line pid owner
    line="$(ss -H -ltnp "( sport = :$port )" 2>/dev/null | head -1)"
    if [[ -n "$line" ]]; then
        if [[ "$line" =~ pid=([0-9]+) ]]; then
            pid="${BASH_REMATCH[1]}"
            owner="$(ps -o comm=,user= -p "$pid" 2>/dev/null)"
            if [[ -n "$owner" ]]; then
                printf '%s (pid %s)' "$owner" "$pid"
                return 0
            fi
        fi
        printf 'another process on this shared host (owner not visible without root)'
        return 0
    fi
    printf 'an unknown process'
}

# ask_port <prompt> <default> -> like ask(), but loops until the chosen port
# is actually free, suggesting the next free one as the new default each time
# and explaining what's occupying the rejected port (not just "pick another").
ask_port() {
    local prompt="$1" default="$2" port suggestion
    while true; do
        port="$(ask "$prompt" "$default")"
        if port_in_use "$port"; then
            suggestion="$(find_free_port $((port + 1)))"
            warn "port $port is already held by $(describe_port_owner "$port") — try $suggestion, or another port of your own choice."
            default="$suggestion"
            continue
        fi
        printf '%s\n' "$port"
        return 0
    done
}

# resolve_ports_silently — non-interactive equivalent of ask_port: if a
# chosen port (default or --app-port/--backend-port) is already taken,
# switch to the next free one instead of failing, but always explain why
# (which process is occupying it, where discoverable) and surface which
# ports actually ended up being used (see main()'s "silent mode" info line).
resolve_ports_silently() {
    local original
    if port_in_use "$APP_PORT"; then
        original="$APP_PORT"
        warn "app port $original is already held by $(describe_port_owner "$original")."
        APP_PORT="$(find_free_port "$APP_PORT")"
        warn "switched app port to $APP_PORT instead."
    fi
    if port_in_use "$BACKEND_PORT" || [[ "$BACKEND_PORT" == "$APP_PORT" ]]; then
        original="$BACKEND_PORT"
        if [[ "$BACKEND_PORT" == "$APP_PORT" ]]; then
            warn "backend port $original collides with the app port."
        else
            warn "backend port $original is already held by $(describe_port_owner "$original")."
        fi
        BACKEND_PORT="$(find_free_port "$BACKEND_PORT")"
        [[ "$BACKEND_PORT" == "$APP_PORT" ]] && BACKEND_PORT="$(find_free_port $((BACKEND_PORT + 1)))"
        warn "switched backend port to $BACKEND_PORT instead."
    fi
}

# ---- Interactive prompts ---------------------------------------------------------
ask() {
    # ask <prompt> <default> -> echoes the chosen value
    local prompt="$1" default="$2" reply
    read -r -p "$(printf '%s?%s %s %s[%s]%s ' "$CYAN$BOLD" "$RESET" "$prompt" "$DIM" "$default" "$RESET")" reply || true
    printf '%s\n' "${reply:-$default}"
}

confirm() {
    local prompt="$1" default="${2:-Y}" reply
    read -r -p "$(printf '%s?%s %s %s[%s/%s]%s ' "$CYAN$BOLD" "$RESET" "$prompt" "$DIM" \
        "$([[ $default == Y ]] && echo Y || echo y)" "$([[ $default == N ]] && echo N || echo n)" "$RESET")" reply || true
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

db_interactive_configure() {
    echo
    info "Database (used for Lens's own structured data -- clusters, deployments, etc.)"
    if confirm "Use built-in embedded PostgreSQL" "Y"; then
        DB_MODE="embedded"
        echo
        info "The embedded database is only reachable via a local Unix socket, but a password is still required (defense in depth)."
        read -r -s -p "$(printf '%s?%s Embedded database password %s(leave blank to auto-generate)%s ' "$CYAN$BOLD" "$RESET" "$DIM" "$RESET")" DB_EMBEDDED_PASSWORD || true
        echo
        return 0
    fi
    DB_MODE="external"
    echo "    engines: postgresql, mysql, oracle, mssql"
    while true; do
        DB_ENGINE="$(ask "Database engine" "$DB_ENGINE")"
        local default_port
        case "$DB_ENGINE" in
            mysql)      default_port="3306" ;;
            oracle)     default_port="1521" ;;
            mssql)      default_port="1433" ;;
            *)          default_port="5432" ;;
        esac
        DB_HOST="$(ask "Database host" "${DB_HOST}")"
        DB_PORT="$(ask "Database port" "${DB_PORT:-$default_port}")"
        DB_NAME="$(ask "Database name" "${DB_NAME:-lens}")"
        DB_USER="$(ask "Database username" "${DB_USER}")"
        read -r -s -p "$(printf '%s?%s Database password %s(hidden)%s ' "$CYAN$BOLD" "$RESET" "$DIM" "$RESET")" DB_PASSWORD || true
        echo

        if test_external_db_connection; then
            ok "connected to $DB_ENGINE at $DB_HOST:$DB_PORT/$DB_NAME"
            return 0
        fi

        if confirm "Use the built-in embedded PostgreSQL instead" "N"; then
            DB_MODE="embedded"
            echo
            info "The embedded database is only reachable via a local Unix socket, but a password is still required (defense in depth)."
            read -r -s -p "$(printf '%s?%s Embedded database password %s(leave blank to auto-generate)%s ' "$CYAN$BOLD" "$RESET" "$DIM" "$RESET")" DB_EMBEDDED_PASSWORD || true
            echo
            return 0
        fi
        info "let's try the external database details again."
    done
}

# test_external_db_connection — probes the DB_* connection details the user
# just entered before letting the installer proceed. Needs the app's own
# virtualenv (SQLAlchemy + drivers), so it provisions the venv early (both
# sync_source and ensure_venv are idempotent and safe to call again later in
# main()'s normal flow). Returns 0 iff the connection succeeds; on failure it
# prints the error and returns 1 so the caller can offer a retry.
test_external_db_connection() {
    sync_source
    ensure_venv
    info "testing connection to $DB_ENGINE at $DB_HOST:$DB_PORT/$DB_NAME ..."
    local args=(--test-connection --mode external --engine "$DB_ENGINE" --host "$DB_HOST" --dbname "$DB_NAME")
    [[ -n "$DB_PORT" ]] && args+=(--port "$DB_PORT")
    [[ -n "$DB_USER" ]] && args+=(--username "$DB_USER")
    [[ -n "$DB_PASSWORD" ]] && args+=(--password "$DB_PASSWORD")
    if ! (cd "$INSTALL_DIR" && .venv/bin/python -m llm_d_bench.db.bootstrap_cli "${args[@]}"); then
        fail "could not connect to that database -- double check the host/port/credentials and that it's reachable from this machine."
        return 1
    fi
}

interactive_configure() {
    step "Configuration"
    info "Press Enter to accept the default shown in brackets."
    INSTALL_DIR="$(ask "Install directory" "$INSTALL_DIR")"
    APP_PORT="$(ask_port "Web app / API port" "$APP_PORT")"
    BACKEND_PORT="$(ask_port "Internal backend port" "$BACKEND_PORT")"
    while [[ "$BACKEND_PORT" == "$APP_PORT" ]]; do
        warn "backend port can't be the same as the app port — pick another."
        BACKEND_PORT="$(ask_port "Internal backend port" "$(find_free_port $((APP_PORT + 1)))")"
    done
    if confirm "Enable HTTPS (self-signed cert; needed for browser mic access etc. off localhost)" "Y"; then
        ENABLE_HTTPS=1
    else
        ENABLE_HTTPS=0
    fi

    db_interactive_configure

    echo
    info "Summary:"
    printf '    install dir     %s%s%s\n' "$BOLD" "$INSTALL_DIR" "$RESET"
    printf '    app port        %s%s%s\n' "$BOLD" "$APP_PORT" "$RESET"
    printf '    backend port    %s%s%s\n' "$BOLD" "$BACKEND_PORT" "$RESET"
    printf '    https           %s%s%s\n' "$BOLD" "$([[ "$ENABLE_HTTPS" == "1" ]] && echo enabled || echo disabled)" "$RESET"
    printf '    database        %s%s%s\n' "$BOLD" "$([[ "$DB_MODE" == "embedded" ]] && echo "built-in embedded PostgreSQL" || echo "$DB_ENGINE at $DB_HOST:$DB_PORT/$DB_NAME")" "$RESET"
    echo
    if ! confirm "Proceed with installation" "Y"; then
        warn "aborted by user."
        exit 1
    fi
    if confirm "Start Lens after installing" "Y"; then
        START_AFTER_INSTALL=1
    else
        START_AFTER_INSTALL=0
    fi
}

# ---- System package prerequisites ------------------------------------------------
ensure_system_packages() {
    step "Checking system packages"
    local need=()
    for pkg_bin in "curl:curl" "tar:tar" "git:git" "python3:python3" "ss:iproute2" "openssl:openssl"; do
        local bin="${pkg_bin%%:*}" pkg="${pkg_bin##*:}"
        command -v "$bin" >/dev/null 2>&1 || need+=("$pkg")
    done
    # python3-venv is a separate Debian/Ubuntu package from python3 itself.
    if ! python3 -c 'import venv' >/dev/null 2>&1; then
        need+=("python3-venv" "python3-pip")
    fi
    command -v pip3 >/dev/null 2>&1 || need+=("python3-pip")
    command -v rsync >/dev/null 2>&1 || need+=("rsync")

    # de-duplicate
    if [[ ${#need[@]} -eq 0 ]]; then
        ok "all required system packages are present"
        return 0
    fi

    local uniq_need
    uniq_need="$(printf '%s\n' "${need[@]}" | sort -u | tr '\n' ' ')"
    warn "missing system packages: $uniq_need"

    if [[ "$ALLOW_APT" != "1" ]]; then
        fail "cannot install missing packages (--no-apt set). Install manually: sudo apt-get install -y $uniq_need"
        exit 1
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        fail "apt-get not found — install manually: $uniq_need"
        exit 1
    fi

    local sudo_cmd=""
    if [[ "$EUID" -ne 0 ]]; then
        command -v sudo >/dev/null 2>&1 || { fail "sudo is required to install packages as non-root"; exit 1; }
        sudo_cmd="sudo"
    fi

    if [[ "$SILENT" != "1" ]]; then
        confirm "Install missing packages with 'apt-get' now" "Y" || { fail "cannot continue without required packages."; exit 1; }
    fi

    info "running: ${sudo_cmd:+$sudo_cmd }apt-get update && apt-get install -y $uniq_need"
    # shellcheck disable=SC2086
    $sudo_cmd apt-get update -qq
    # shellcheck disable=SC2086
    $sudo_cmd apt-get install -y -qq $uniq_need
    ok "system packages installed"
}

# ---- Node.js toolchain (adapted from scripts/dev.sh) ------------------------------
TOOLS_DIR="$INSTALL_DIR/.tools"
LOCAL_NODE_DIR="$TOOLS_DIR/node"

node_major() {
    command -v node >/dev/null 2>&1 || return 0
    node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

node_is_supported() {
    local major
    major="$(node_major)"
    [[ -n "$major" ]] && [[ "$major" -ge "$NODE_MIN_MAJOR" ]]
}

activate_local_node() {
    [[ -x "$LOCAL_NODE_DIR/bin/node" ]] || return 1
    case ":$PATH:" in
        *":$LOCAL_NODE_DIR/bin:"*) ;;
        *) PATH="$LOCAL_NODE_DIR/bin:$PATH"; export PATH ;;
    esac
    return 0
}

resolve_node_version() {
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

install_node() {
    local platform="linux-x64" version tarball url workdir
    for tool in curl tar; do
        command -v "$tool" >/dev/null 2>&1 || { fail "$tool is required to install Node.js"; return 1; }
    done
    command -v sha256sum >/dev/null 2>&1 || { fail "sha256sum is required to verify the Node.js download"; return 1; }

    version="$(resolve_node_version)"
    tarball="node-$version-$platform.tar.xz"
    url="$NODE_DIST_URL/$version/$tarball"
    info "installing Node.js $version into $LOCAL_NODE_DIR ..."

    mkdir -p "$TOOLS_DIR"
    workdir="$(mktemp -d "$TOOLS_DIR/node-download.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '$workdir'" RETURN

    curl --proto '=https' --tlsv1.2 -fL --progress-bar -m 600 -o "$workdir/$tarball" "$url" \
        || { fail "failed to download $url"; return 1; }
    curl --proto '=https' --tlsv1.2 -fL -sS -m 60 -o "$workdir/SHASUMS256.txt" "$NODE_DIST_URL/$version/SHASUMS256.txt" \
        || { fail "failed to download the checksum list for $version"; return 1; }

    local expected actual
    expected="$(awk -v file="$tarball" '$2 == file {print $1}' "$workdir/SHASUMS256.txt")"
    [[ -n "$expected" ]] || { fail "$tarball is not listed in SHASUMS256.txt"; return 1; }
    actual="$(sha256sum "$workdir/$tarball" | awk '{print $1}')"
    [[ "$expected" == "$actual" ]] || { fail "checksum mismatch for $tarball"; return 1; }

    tar -xJf "$workdir/$tarball" -C "$workdir" || { fail "failed to extract $tarball"; return 1; }

    local target="$TOOLS_DIR/node-$version-$platform"
    rm -rf "$target"
    mv "$workdir/node-$version-$platform" "$target"
    ln -sfn "$target" "$LOCAL_NODE_DIR"

    activate_local_node || { fail "installed Node.js is not executable at $LOCAL_NODE_DIR/bin/node"; return 1; }
    ok "Node.js ready: $(node --version) (npm $(npm --version 2>/dev/null || echo '?'))"
}

ensure_node() {
    step "Node.js toolchain"
    if node_is_supported; then
        ok "system Node.js $(node --version) is supported"
        return 0
    fi
    if activate_local_node && node_is_supported; then
        ok "using previously installed local Node.js $(node --version)"
        return 0
    fi
    warn "Node.js >= $NODE_MIN_MAJOR.x not found — installing a local copy under $LOCAL_NODE_DIR"
    install_node
}

# ---- Copy source & build ---------------------------------------------------------
sync_source() {
    step "Installing files to $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    if [[ "$(cd "$INSTALL_DIR" && pwd)" == "$SOURCE_DIR" ]]; then
        ok "installing in place ($SOURCE_DIR)"
        return 0
    fi
    rsync -a --delete \
        --exclude '.git' --exclude 'node_modules' --exclude '.venv' --exclude 'dist' \
        --exclude '.dev-pids' --exclude '.dev-logs' --exclude '.dev-tools' --exclude '.tools' \
        --exclude '.pytest_cache' --exclude 'session-artifacts' --exclude '.lens-pids' \
        --exclude '.lens-logs' --exclude 'private/run_store' --exclude 'certs' \
        "$SOURCE_DIR"/ "$INSTALL_DIR"/
    ok "copied source tree to $INSTALL_DIR"
}

ensure_venv() {
    step "Python virtual environment"
    cd "$INSTALL_DIR"

    # The embedded PostgreSQL mode needs the optional 'pgserver' extra and the
    # LDAP identity provider needs the optional 'ldap' extra; a plain
    # "pip install -e ." skips both. Install them so those features work.
    local extras=()
    [[ "$DB_MODE" == "embedded" ]] && extras+=("embedded-db")
    extras+=("ldap")
    local extra="[$(IFS=,; printf '%s' "${extras[*]}")]"

    # A stamp of pyproject.toml (plus the selected extra) forces a reinstall
    # when a dependency is added/bumped by a source update, e.g. casbin. Without
    # it, an existing .venv that still imports llm_d_bench/uvicorn/pgserver
    # would skip the install and the database bootstrap would fail on the new
    # module. sync_source excludes .venv, so the stamp survives re-installs.
    local stamp=".venv/.install-stamp"
    local current_hash=""
    if [[ -f pyproject.toml ]]; then
        current_hash="${extra}:$(sha256sum pyproject.toml 2>/dev/null | awk '{print $1}')"
    fi

    local check_import="import llm_d_bench, uvicorn"
    [[ "$DB_MODE" == "embedded" ]] && check_import="$check_import, pgserver"
    if [[ -x .venv/bin/python && -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" == "$current_hash" ]] \
        && .venv/bin/python -c "$check_import" 2>/dev/null; then
        ok "virtualenv already set up"
        return 0
    fi
    if [[ ! -x .venv/bin/python ]]; then
        info "creating virtualenv (.venv)..."
        python3 -m venv .venv || { fail "python3 -m venv failed"; exit 1; }
    elif [[ -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" != "$current_hash" ]]; then
        info "pyproject.toml changed since the last install — refreshing Python dependencies..."
    fi
    info "installing Python dependencies (pip install -e .${extra})..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
    run_with_tail 5 .venv/bin/python -m pip install -e ".${extra}" || { fail "pip install -e .${extra} failed"; exit 1; }
    echo "$current_hash" > "$stamp"
    ok "Python dependencies installed"
}

# ---- Application database ("Step 0") ---------------------------------------------
# This used to be a mandatory gate inside CreateClusterWizard.jsx (blocking
# the wizard until configured). It now happens once here, before the backend
# is ever started, so the wizard can simply assume the database is ready —
# see llm_d_bench/db/bootstrap_cli.py.
configure_database() {
    step "Configuring the Lens database"
    cd "$INSTALL_DIR"
    local args=(--mode "$DB_MODE")
    if [[ "$DB_MODE" == "external" ]]; then
        if [[ -z "$DB_HOST" || -z "$DB_NAME" ]]; then
            fail "--db-mode external requires --db-host and --db-name (or run interactively)."
            exit 1
        fi
        args+=(--engine "$DB_ENGINE" --host "$DB_HOST" --dbname "$DB_NAME")
        [[ -n "$DB_PORT" ]] && args+=(--port "$DB_PORT")
        [[ -n "$DB_USER" ]] && args+=(--username "$DB_USER")
        [[ -n "$DB_PASSWORD" ]] && args+=(--password "$DB_PASSWORD")
    else
        if [[ -z "$DB_EMBEDDED_PASSWORD" ]]; then
            DB_EMBEDDED_PASSWORD="$(openssl rand -base64 24)"
            DB_EMBEDDED_PASSWORD_AUTOGEN=1
            info "generated a random embedded database password (stored in ~/.llm-d-lens/db-bootstrap.json)"
        fi
        args+=(--embedded-password "$DB_EMBEDDED_PASSWORD")
    fi
    if ! .venv/bin/python -m llm_d_bench.db.bootstrap_cli "${args[@]}"; then
        fail "database configuration failed — re-run with --db-mode embedded to use the built-in PostgreSQL, or fix the external database settings."
        exit 1
    fi
    ok "database configured (mode: $DB_MODE)"
}

build_app() {
    step "Building the web app"
    cd "$INSTALL_DIR"
    info "running npm install ..."
    # --ignore-scripts: onnxruntime-node's postinstall tries to download a
    # native binary that's unreachable on some networks; harmless to skip
    # since the browser build (transformers.js) never uses it anyway.
    npm install --no-audit --no-fund --ignore-scripts || { fail "npm install failed"; exit 1; }
    info "running npm run build ..."
    npm run build || { fail "npm run build failed"; exit 1; }
    ok "build complete (dist/)"
}

# ---- Config & control script ------------------------------------------------------
setup_tls() {
    [[ "$ENABLE_HTTPS" == "1" ]] || return 0
    step "Setting up HTTPS (self-signed certificate)"
    local host="$TLS_HOST"
    if [[ -z "$host" ]]; then
        host="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [[ -z "$host" ]]; then
        warn "could not detect a LAN IP for the TLS certificate — falling back to plain HTTP."
        warn "re-run with --tls-host <your-ip> to enable HTTPS."
        ENABLE_HTTPS=0
        return 0
    fi
    local cert_env
    if ! cert_env="$("$INSTALL_DIR/scripts/generate-self-signed-cert.sh" "$host" "$INSTALL_DIR/certs")"; then
        warn "failed to generate a self-signed TLS certificate — falling back to plain HTTP."
        ENABLE_HTTPS=0
        return 0
    fi
    eval "$cert_env"
    ok "certificate ready for $host ($INSTALL_DIR/certs)"
}

write_config() {
    step "Writing configuration"
    local in_place=0
    [[ "$(cd "$INSTALL_DIR" && pwd)" == "$SOURCE_DIR" ]] && in_place=1
    cat > "$INSTALL_DIR/.lens-install.env" <<EOF
# Generated by LensInstaller-Ubuntu-x86_64.sh — safe to edit and re-run lensctl.sh.
APP_PORT=$APP_PORT
BACKEND_PORT=$BACKEND_PORT
APP_HOST=0.0.0.0
LENS_IN_PLACE=$in_place
ENABLE_HTTPS=$ENABLE_HTTPS
TLS_CERT_FILE=${TLS_CERT_FILE:-}
TLS_KEY_FILE=${TLS_KEY_FILE:-}
DB_MODE=$DB_MODE
EOF
    ok "wrote $INSTALL_DIR/.lens-install.env"
}

write_control_script() {
    cat > "$INSTALL_DIR/lensctl.sh" <<'CTL'
#!/usr/bin/env bash
# lensctl.sh — start/stop/status for an installed llm-d Lens deployment.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
[[ -f .lens-install.env ]] && source .lens-install.env
APP_PORT="${APP_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8081}"
APP_HOST="${APP_HOST:-0.0.0.0}"
LENS_IN_PLACE="${LENS_IN_PLACE:-0}"
ENABLE_HTTPS="${ENABLE_HTTPS:-1}"
TLS_CERT_FILE="${TLS_CERT_FILE:-}"
TLS_KEY_FILE="${TLS_KEY_FILE:-}"
DB_MODE="${DB_MODE:-embedded}"
PID_DIR="$ROOT_DIR/.prism-pids"
source "$ROOT_DIR/scripts/storage-env.sh"
LOG_DIR="$LENS_LOG_DIR/service"
EVALUATE_STORE="$LENS_DATA_DIR/metadata/evaluations"

if [[ -x "$ROOT_DIR/.tools/node/bin/node" ]]; then
    PATH="$ROOT_DIR/.tools/node/bin:$PATH"
fi

log()  { printf '\033[1;36m[lens]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[lens]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[lens]\033[0m %s\n' "$*" >&2; }

pid_file() { printf '%s/%s.pid' "$PID_DIR" "$1"; }
is_running() {
    local pid; pid="$(cat "$(pid_file "$1")" 2>/dev/null || echo '')"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}
# This host may be shared with other users/instances, so a previously chosen
# port can end up already bound by someone/something else by the time this
# runs again -- check right before starting rather than finding out from a
# crashed uvicorn/node log after the fact.
port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -H -ltn "( sport = :$port )" 2>/dev/null | grep -q .
        return
    fi
    (: < "/dev/tcp/127.0.0.1/$port") 2>/dev/null
}
find_free_port() {
    local port="$1" tries=0
    while port_in_use "$port"; do
        port=$((port + 1))
        tries=$((tries + 1))
        if [[ $tries -ge 200 ]]; then
            fail "could not find a free port at or after $1 (checked 200 ports)"
            exit 1
        fi
    done
    printf '%s\n' "$port"
}
# describe_port_owner <port> -> best-effort "who's holding this port" string;
# on this kind of shared host the occupant is often another user's process,
# whose PID `ss` won't reveal without root, so say that plainly instead of
# guessing.
describe_port_owner() {
    local port="$1" line pid owner
    line="$(ss -H -ltnp "( sport = :$port )" 2>/dev/null | head -1)"
    if [[ -n "$line" ]]; then
        if [[ "$line" =~ pid=([0-9]+) ]]; then
            pid="${BASH_REMATCH[1]}"
            owner="$(ps -o comm=,user= -p "$pid" 2>/dev/null)"
            if [[ -n "$owner" ]]; then
                printf '%s (pid %s)' "$owner" "$pid"
                return 0
            fi
        fi
        printf 'another process on this shared host (owner not visible without root)'
        return 0
    fi
    printf 'an unknown process'
}
# resolve_ports — auto-switch away from an already-occupied port instead of
# starting a service doomed to crash with EADDRINUSE; explains what's holding
# the old port (not just the new one picked) and reports the actual port
# used (also visible afterwards via .lens-install.env / status).
resolve_ports() {
    local original
    if ! is_running "app" && port_in_use "$APP_PORT"; then
        original="$APP_PORT"
        warn "app port $original is already held by $(describe_port_owner "$original")."
        APP_PORT="$(find_free_port "$APP_PORT")"
        warn "switched app port to $APP_PORT instead."
    fi
    if ! is_running "backend" && { port_in_use "$BACKEND_PORT" || [[ "$BACKEND_PORT" == "$APP_PORT" ]]; }; then
        original="$BACKEND_PORT"
        if [[ "$BACKEND_PORT" == "$APP_PORT" ]]; then
            warn "backend port $original collides with the app port."
        else
            warn "backend port $original is already held by $(describe_port_owner "$original")."
        fi
        BACKEND_PORT="$(find_free_port "$BACKEND_PORT")"
        [[ "$BACKEND_PORT" == "$APP_PORT" ]] && BACKEND_PORT="$(find_free_port $((BACKEND_PORT + 1)))"
        warn "switched backend port to $BACKEND_PORT instead."
    fi
}
# Writes any port change resolve_ports made back to .lens-install.env, so it
# sticks across future stop/start/restart/status invocations instead of
# re-detecting (and re-warning about) the same conflict every time.
persist_ports() {
    [[ -f "$ROOT_DIR/.lens-install.env" ]] || return 0
    local tmp; tmp="$(mktemp "$ROOT_DIR/.lens-install.env.XXXXXX")"
    awk -v app="$APP_PORT" -v backend="$BACKEND_PORT" '
        /^APP_PORT=/     { print "APP_PORT=" app;     next }
        /^BACKEND_PORT=/ { print "BACKEND_PORT=" backend; next }
        { print }
    ' "$ROOT_DIR/.lens-install.env" > "$tmp"
    mv "$tmp" "$ROOT_DIR/.lens-install.env"
}
# Regenerates the self-signed cert if it's missing (e.g. certs/ was deleted),
# so a plain `lensctl.sh start` is always enough — no manual export needed.
ensure_tls() {
    [[ "$ENABLE_HTTPS" == "1" ]] || return 0
    if [[ -n "$TLS_CERT_FILE" && -n "$TLS_KEY_FILE" && -s "$TLS_CERT_FILE" && -s "$TLS_KEY_FILE" ]]; then
        return 0
    fi
    local host; host="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [[ -z "$host" ]]; then
        warn "could not detect a LAN IP for the TLS certificate — running plain HTTP."
        ENABLE_HTTPS=0
        return 0
    fi
    local cert_env
    if ! cert_env="$("$ROOT_DIR/scripts/generate-self-signed-cert.sh" "$host" "$ROOT_DIR/certs")"; then
        warn "failed to generate a self-signed TLS certificate — running plain HTTP."
        ENABLE_HTTPS=0
        return 0
    fi
    eval "$cert_env"
}
start_one() {
    local name="$1"; shift
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
    local name="$1" url="$2" attempts="${3:-30}" i=0
    while [[ $i -lt $attempts ]]; do
        curl -sf -k -m 2 "$url" >/dev/null 2>&1 && return 0
        sleep 1; i=$((i + 1))
    done
    warn "$name did not become ready at $url — see $LOG_DIR/$name.log"
    return 1
}

# Regenerates server/mcp/tools.ts from the FastAPI backend's live OpenAPI
# schema before every start, so the MCP tool catalog can never drift from the
# routes actually exposed by this installed backend. Non-fatal: if it fails
# (e.g. venv/node_modules missing), fall back to whatever tools.ts already
# ships with this install.
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
cmd_start() {
    resolve_ports
    persist_ports
    ensure_tls
    regenerate_mcp_tools
    local scheme="http"
    [[ "$ENABLE_HTTPS" == "1" ]] && scheme="https"
    start_one backend env \
        PRISM_MCP_URL="$scheme://127.0.0.1:$APP_PORT/api/mcp" \
        PRISM_MCP_CA_FILE="${TLS_CERT_FILE:-}" \
        .venv/bin/python -m uvicorn llm_d_bench.api:app --host "${BACKEND_HOST:-0.0.0.0}" --port "$BACKEND_PORT"
    start_one app env NODE_ENV=production HOST="$APP_HOST" SERVER_PORT="$APP_PORT" PORT="$APP_PORT" \
        SIMULATION_API_URL="http://127.0.0.1:$BACKEND_PORT" \
        TLS_CERT_FILE="$TLS_CERT_FILE" TLS_KEY_FILE="$TLS_KEY_FILE" \
        ./node_modules/.bin/tsx server/server.js
    log "waiting for services to become ready..."
    wait_for_http backend "http://127.0.0.1:$BACKEND_PORT/api/health"
    wait_for_http app     "$scheme://127.0.0.1:$APP_PORT/api/config"
    local lan_ip
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    log "llm-d Lens is running:"
    log "  app       $scheme://localhost:$APP_PORT  (bound to $APP_HOST)"
    [[ -n "$lan_ip" && "$APP_HOST" == "0.0.0.0" ]] && log "             $scheme://$lan_ip:$APP_PORT  (LAN)"
    log "  backend   http://127.0.0.1:$BACKEND_PORT (internal)"
    if [[ "$ENABLE_HTTPS" == "1" ]]; then
        log "  HTTPS uses a self-signed cert — your browser will warn once; accept it to proceed. Needed for mic access etc. off localhost."
    fi
}

# embedded_db_dir -> the on-disk location of the built-in PostgreSQL cluster
# (see llm_d_bench/db/settings.py's _DB_DATA_DIRECTORY_DEFAULT), honoring the
# same LLM_D_BENCH_EMBEDDED_DB_DIR override the backend itself respects.
embedded_db_dir() {
    local dir="${LLM_D_BENCH_EMBEDDED_DB_DIR:-~/.llm-d-lens/db/data}"
    printf '%s\n' "${dir/#\~/$HOME}"
}

# embedded_db_bootstrap_file -> the "Step 0" config recording how this install
# connects to its own database (see llm_d_bench/db/bootstrap_config.py),
# honoring the same LLM_D_BENCH_DB_BOOTSTRAP_FILE override the backend respects.
embedded_db_bootstrap_file() {
    local file="${LLM_D_BENCH_DB_BOOTSTRAP_FILE:-~/.llm-d-lens/db-bootstrap.json}"
    printf '%s\n' "${file/#\~/$HOME}"
}

# stop_embedded_postgres_process — best-effort graceful stop of the embedded
# PostgreSQL started by pgserver inside the backend process. Killing the
# backend's uvicorn pid does not guarantee its pgserver-managed postgres
# subprocess exits too, so read its postmaster.pid and signal it directly;
# otherwise it's left running as an orphan after `stop`/`uninstall`.
stop_embedded_postgres_process() {
    [[ "$DB_MODE" == "embedded" ]] || return 0
    local data_dir pid_file pid waited=0
    data_dir="$(embedded_db_dir)"
    pid_file="$data_dir/postmaster.pid"
    [[ -f "$pid_file" ]] || return 0
    pid="$(head -n1 "$pid_file" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 0
    log "stopping embedded PostgreSQL (pid $pid)..."
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

cmd_stop() {
    for svc in backend app; do
        local pf; pf="$(pid_file "$svc")"
        if [[ -f "$pf" ]]; then
            local pid; pid="$(cat "$pf")"
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                log "stopping $svc (pid $pid)..."
                kill "$pid" 2>/dev/null || true
            fi
            rm -f "$pf"
        fi
    done
    stop_embedded_postgres_process
    log "stopped."
}

cmd_status() {
    for svc in backend app; do
        if is_running "$svc"; then
            log "  $svc: running (pid $(cat "$(pid_file "$svc")"))"
        else
            log "  $svc: stopped"
        fi
    done
}

# cmd_uninstall — stop services and remove the installation. By default, user
# legacy repository-local data and the port config are kept by default.
# --purge removes the installation only; external LENS_* data roots are retained.
# The embedded database (if this install used --db-mode embedded) is asked
# about separately via --keep-db/--purge-db: its process is always stopped
# regardless of that answer, only its on-disk data is conditionally removed.
cmd_uninstall() {
    local mode="ask" db_mode="ask"
    for arg in "$@"; do
        case "$arg" in
            --keep-data) mode="keep" ;;
            --purge)     mode="purge" ;;
            --keep-db)   db_mode="keep" ;;
            --purge-db)  db_mode="purge" ;;
            -y|--yes)
                [[ "$mode" == "ask" ]] && mode="keep"
                [[ "$db_mode" == "ask" ]] && db_mode="keep"
                ;;
            *) fail "unknown uninstall option: $arg"; exit 1 ;;
        esac
    done

    if [[ "$mode" == "ask" ]]; then
        if [[ -t 0 ]]; then
            local reply
            read -r -p "Keep legacy installation data and port config? [Y/n] " reply || true
            reply="${reply:-Y}"
            [[ "$reply" =~ ^[Yy] ]] && mode="keep" || mode="purge"
        else
            warn "no TTY for prompt — defaulting to keep data (use --purge to remove it)."
            mode="keep"
        fi
    fi

    if [[ "$DB_MODE" == "embedded" && "$db_mode" == "ask" ]]; then
        if [[ -t 0 ]]; then
            local db_reply
            read -r -p "Delete the embedded database at $(embedded_db_dir)? [y/N] " db_reply || true
            db_reply="${db_reply:-N}"
            [[ "$db_reply" =~ ^[Yy] ]] && db_mode="purge" || db_mode="keep"
        else
            warn "no TTY for prompt — defaulting to keep the embedded database (use --purge-db to remove it)."
            db_mode="keep"
        fi
    fi

    log "External Lens data remains at $LENS_DATA_DIR (not removed by uninstall)."
    log "stopping services..."
    cmd_stop

    if [[ "$DB_MODE" == "embedded" ]]; then
        if [[ "$db_mode" == "purge" ]]; then
            log "deleting embedded database at $(embedded_db_dir)..."
            rm -rf "$(embedded_db_dir)"
            rm -f "$(embedded_db_bootstrap_file)"
            log "embedded database deleted."
        else
            log "embedded database stopped; data kept at $(embedded_db_dir)."
        fi
    fi

    if [[ "$LENS_IN_PLACE" == "1" ]]; then
        warn "this is an in-place install (running from the source checkout) —"
        warn "only build artifacts will be removed; source files are left untouched."
        rm -rf node_modules dist .venv .tools "$PID_DIR"
        [[ "$mode" == "purge" ]] && rm -f .lens-install.env
        log "build artifacts removed from $ROOT_DIR."
        return 0
    fi

    if [[ "$mode" == "keep" ]]; then
        log "removing installed app, keeping private/run_store and .lens-install.env..."
        find "$ROOT_DIR" -mindepth 1 -maxdepth 1 \
            ! -name 'private' ! -name '.lens-install.env' -exec rm -rf {} +
        log "done. Data preserved at $ROOT_DIR/private and $ROOT_DIR/.lens-install.env"
        log "remove that directory manually if you no longer need the data."
    else
        log "purging everything, including private/run_store..."
        cd /
        rm -rf "$ROOT_DIR"
        log "done. $ROOT_DIR has been removed."
    fi
}

case "${1:-}" in
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_stop; cmd_start ;;
    status)    cmd_status ;;
    uninstall) shift; cmd_uninstall "$@" ;;
    *) echo "Usage: $0 {start|stop|restart|status|uninstall [--keep-data|--purge|-y]}"; exit 1 ;;
esac
CTL
    chmod +x "$INSTALL_DIR/lensctl.sh"
    ok "wrote $INSTALL_DIR/lensctl.sh"
}

# ---- Main -------------------------------------------------------------------------
run_uninstall() {
    print_logo
    if [[ "$SILENT" != "1" ]]; then
        INSTALL_DIR="$(ask "Directory to uninstall from" "$INSTALL_DIR")"
    fi
    if [[ ! -x "$INSTALL_DIR/lensctl.sh" ]]; then
        fail "no llm-d Lens installation found at $INSTALL_DIR (lensctl.sh missing)."
        exit 1
    fi
    if [[ "$SILENT" != "1" && ${#UNINSTALL_ARGS[@]} -eq 0 ]]; then
        if ! confirm "Uninstall llm-d Lens from $INSTALL_DIR" "N"; then
            warn "aborted by user."
            exit 1
        fi
    fi
    "$INSTALL_DIR/lensctl.sh" uninstall "${UNINSTALL_ARGS[@]}"
}

main() {
    if [[ "$UNINSTALL" == "1" ]]; then
        run_uninstall
        return 0
    fi

    print_logo
    require_ubuntu
    require_arch

    if [[ "$SILENT" != "1" ]]; then
        interactive_configure
    else
        resolve_ports_silently
        info "silent mode — install dir: $INSTALL_DIR, app port: $APP_PORT, backend port: $BACKEND_PORT"
    fi

    ensure_system_packages
    TOOLS_DIR="$INSTALL_DIR/.tools"
    LOCAL_NODE_DIR="$TOOLS_DIR/node"
    ensure_node
    sync_source
    ensure_venv
    configure_database
    build_app
    setup_tls
    write_config
    write_control_script

    step "Done"
    ok "llm-d Lens installed at $INSTALL_DIR"
    if [[ "$DB_MODE" == "embedded" && "$DB_EMBEDDED_PASSWORD_AUTOGEN" == "1" ]]; then
        printf '%s[lens]%s %s⚠%s auto-generated embedded database password (save this — it will not be shown again):\n' \
            "$YELLOW$BOLD" "$RESET" "$YELLOW" "$RESET"
        printf '    %s%s%s\n' "$BOLD" "$DB_EMBEDDED_PASSWORD" "$RESET"
        info "also recoverable later from ~/.llm-d-lens/db-bootstrap.json on this machine."
    fi
    info "manage it with:"
    printf '    %s%s/lensctl.sh start|stop|restart|status|uninstall%s\n' "$BOLD" "$INSTALL_DIR" "$RESET"

    if [[ "$START_AFTER_INSTALL" == "1" ]]; then
        step "Starting llm-d Lens"
        "$INSTALL_DIR/lensctl.sh" start
    else
        info "not starting automatically — run '$INSTALL_DIR/lensctl.sh start' when ready."
    fi
}

main "$@"
