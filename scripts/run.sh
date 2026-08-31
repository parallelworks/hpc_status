#!/usr/bin/env bash
#
# HPC Status Monitor - Run Script
#
# Handles virtual environment setup and dependency management using uv.
# Uses a shared venv in ~/.venvs/hpc-status for faster subsequent starts.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Configuration
VENV_DIR="${HPC_STATUS_VENV:-${HOME}/.venvs/hpc-status}"
DATA_DIR="${HPC_STATUS_DATA_DIR:-${HOME}/.hpc_status}"
UV_BIN="${HOME}/.local/bin/uv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Server defaults
# Remember whether the caller chose the port. An explicit port is a
# promise to something else — `pw endpoints run` assigns one and tunnels
# to it — so we must never quietly move off it.
if [ -n "${PORT+set}" ]; then PORT_EXPLICIT=1; else PORT_EXPLICIT=0; fi
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
# Under `pw endpoints run` the public URL may be path-based
# (/me/session/<user>/<name>/); PW_ENDPOINT_PATH is what the platform
# forwards under, and the app needs it to build its own links. It is "/"
# for a subdomain endpoint, which the server treats as no prefix at all.
URL_PREFIX="${URL_PREFIX:-${PW_ENDPOINT_PATH:-}}"
# Default theme: leave empty so the config's ``ui.default_theme`` wins.
# Set DEFAULT_THEME=dark|light in the environment only when you want to
# override the config (e.g. for a one-off test launch).
DEFAULT_THEME="${DEFAULT_THEME:-}"
CONFIG_FILE="${CONFIG_FILE:-${HPC_STATUS_CONFIG:-}}"

# Feature flags
ENABLE_CLUSTER_PAGES="${ENABLE_CLUSTER_PAGES:-1}"
ENABLE_CLUSTER_MONITOR="${ENABLE_CLUSTER_MONITOR:-${ENABLE_CLUSTER_PAGES}}"
CLUSTER_MONITOR_INTERVAL="${CLUSTER_MONITOR_INTERVAL:-120}"
# Blank means "whatever the config says".
MAX_CONCURRENT_SSH="${MAX_CONCURRENT_SSH:-}"

cd "${PROJECT_ROOT}"

# Install uv if not present
install_uv() {
    if [ -f "$UV_BIN" ]; then
        return 0
    fi
    echo "[run] Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
}

# Create shared venv if not exists
setup_venv() {
    if [ -d "$VENV_DIR" ]; then
        echo "[run] Using existing venv: $VENV_DIR"
        return 0
    fi
    echo "[run] Creating shared venv: $VENV_DIR"
    $UV_BIN venv "$VENV_DIR"
}

# Sync dependencies
sync_deps() {
    echo "[run] Syncing dependencies..."

    # Always ensure core dependencies are installed first
    echo "[run] Installing core dependencies..."
    $UV_BIN pip install --python "$VENV_DIR/bin/python" \
        requests beautifulsoup4 urllib3 certifi pyyaml 2>/dev/null || \
    "$VENV_DIR/bin/pip" install requests beautifulsoup4 urllib3 certifi pyyaml

    # Install the project in editable mode
    echo "[run] Installing project..."
    $UV_BIN pip install --python "$VENV_DIR/bin/python" -e "$PROJECT_ROOT" 2>/dev/null || \
    "$VENV_DIR/bin/pip" install -e "$PROJECT_ROOT"
}

# Create data directory
setup_data_dir() {
    mkdir -p "$DATA_DIR/logs" "$DATA_DIR/cache" "$DATA_DIR/markdown" "$DATA_DIR/user_data"
}

# Stop a previous instance of *this* dashboard on the port.
#
# The old version killed whatever held the port, which is fine on a laptop
# and wrong on a shared workspace: port 8080 on an ACTIVATE node belongs to
# Grafana. Match our own command line instead, so we can only ever stop
# ourselves.
cleanup_existing() {
    local pids
    pids=$(pgrep -u "$(id -u)" -f -- "src\.server\.main .*--port ${PORT}( |\$)" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "[run] Stopping previous dashboard on port ${PORT} (pid: ${pids//$'\n'/, })"
        kill $pids 2>/dev/null || true
        # Give it a moment to release the socket before we bind it.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            port_is_free && return 0
            sleep 0.3
        done
    fi
}

# True when nothing is listening on $1 (or $PORT). Uses bash's /dev/tcp so
# it works without lsof, netstat, or ss.
port_is_free() {
    local probe="${1:-$PORT}"
    ! (exec 3<>"/dev/tcp/127.0.0.1/${probe}") 2>/dev/null
}

# A default port is a suggestion; an explicit one is a contract.
select_port() {
    port_is_free && return 0

    if [[ "$PORT_EXPLICIT" == "1" ]]; then
        # Leave it: the server prints a far better diagnostic than we can,
        # including what is holding the port.
        return 0
    fi

    local candidate
    for candidate in $(seq $((PORT + 1)) $((PORT + 20))); do
        if port_is_free "$candidate"; then
            echo "[run] Port ${PORT} is in use by another service — using ${candidate}"
            echo "[run] Set PORT=... to choose one yourself"
            PORT="$candidate"
            return 0
        fi
    done
    echo "[run] Ports ${PORT}-$((PORT + 20)) are all in use; set PORT=... to pick one"
}

# Build server command
build_cmd() {
    local cmd=("$VENV_DIR/bin/python" "-m" "src.server.main")
    cmd+=("--host" "$HOST" "--port" "$PORT")
    # Only forward --default-theme when explicitly set, otherwise the config
    # file's ui.default_theme is the authoritative default.
    if [[ -n "$DEFAULT_THEME" ]]; then
        cmd+=("--default-theme" "$DEFAULT_THEME")
    fi

    if [[ -n "$URL_PREFIX" ]]; then
        cmd+=("--url-prefix" "$URL_PREFIX")
    fi

    if [[ -n "$CONFIG_FILE" ]]; then
        cmd+=("--config" "$CONFIG_FILE")
    fi

    local enable_cluster_pages_lc
    enable_cluster_pages_lc=$(printf '%s' "$ENABLE_CLUSTER_PAGES" | tr '[:upper:]' '[:lower:]')
    if [[ "$enable_cluster_pages_lc" =~ ^(0|false|no|off)$ ]]; then
        cmd+=("--disable-cluster-pages")
    else
        cmd+=("--enable-cluster-pages")
    fi

    local enable_cluster_monitor_lc
    enable_cluster_monitor_lc=$(printf '%s' "$ENABLE_CLUSTER_MONITOR" | tr '[:upper:]' '[:lower:]')
    if [[ "$enable_cluster_monitor_lc" =~ ^(0|false|no|off)$ ]]; then
        cmd+=("--disable-cluster-monitor")
    else
        cmd+=("--enable-cluster-monitor")
    fi

    if [[ -n "$CLUSTER_MONITOR_INTERVAL" ]]; then
        cmd+=("--cluster-monitor-interval" "$CLUSTER_MONITOR_INTERVAL")
    fi

    if [[ -n "$MAX_CONCURRENT_SSH" && "$MAX_CONCURRENT_SSH" != "0" ]]; then
        cmd+=("--max-concurrent-ssh" "$MAX_CONCURRENT_SSH")
    fi

    echo "${cmd[@]}"
}

main() {
    echo "============================================"
    echo "  HPC Status Monitor"
    echo "============================================"

    # Setup
    install_uv
    setup_venv
    sync_deps
    setup_data_dir

    # Stop our own previous instance, then settle on a port
    cleanup_existing
    select_port

    # Build and run command
    local cmd
    cmd=$(build_cmd)

    echo "[run] Starting dashboard on ${HOST}:${PORT}"
    [[ -n "$CONFIG_FILE" ]] && echo "[run] Config file: ${CONFIG_FILE}"
    [[ -n "$URL_PREFIX" ]] && echo "[run] URL prefix: ${URL_PREFIX}"
    echo "[run] Data directory: ${DATA_DIR}"
    echo "[run] Command: ${cmd}"
    echo "============================================"

    # Export data directory for the server
    export HPC_STATUS_DATA_DIR="$DATA_DIR"

    # Run the server
    exec $cmd
}

# Run main, unless this file was sourced — the tests source it to exercise
# the port helpers without starting a dashboard.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
