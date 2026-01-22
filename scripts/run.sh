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
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
URL_PREFIX="${URL_PREFIX:-}"
DEFAULT_THEME="${DEFAULT_THEME:-dark}"
CONFIG_FILE="${CONFIG_FILE:-}"

# Feature flags
ENABLE_CLUSTER_PAGES="${ENABLE_CLUSTER_PAGES:-1}"
ENABLE_CLUSTER_MONITOR="${ENABLE_CLUSTER_MONITOR:-${ENABLE_CLUSTER_PAGES}}"
CLUSTER_MONITOR_INTERVAL="${CLUSTER_MONITOR_INTERVAL:-120}"

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
    if [ -f "uv.lock" ]; then
        # Use lockfile for exact versions
        $UV_BIN pip sync --python "$VENV_DIR/bin/python" uv.lock 2>/dev/null || \
        $UV_BIN pip install --python "$VENV_DIR/bin/python" -e .
    else
        # Install from pyproject.toml
        $UV_BIN pip install --python "$VENV_DIR/bin/python" -e .
    fi
}

# Create data directory
setup_data_dir() {
    mkdir -p "$DATA_DIR/logs" "$DATA_DIR/cache" "$DATA_DIR/markdown" "$DATA_DIR/user_data"
}

# Kill any existing dashboard process on the port
cleanup_existing() {
    if command -v netstat &> /dev/null; then
        netstat -tulpn 2>/dev/null | grep ":$PORT " | awk '{print $7}' | cut -d '/' -f1 | xargs -r kill 2>/dev/null || true
    elif command -v lsof &> /dev/null; then
        lsof -ti:$PORT | xargs -r kill 2>/dev/null || true
    fi
}

# Build server command
build_cmd() {
    local cmd=("$VENV_DIR/bin/python" "-m" "src.server.main")
    cmd+=("--host" "$HOST" "--port" "$PORT" "--default-theme" "$DEFAULT_THEME")

    if [[ -n "$URL_PREFIX" ]]; then
        cmd+=("--url-prefix" "$URL_PREFIX")
    fi

    if [[ -n "$CONFIG_FILE" ]]; then
        cmd+=("--config" "$CONFIG_FILE")
    fi

    if [[ "${ENABLE_CLUSTER_PAGES,,}" =~ ^(0|false|no|off)$ ]]; then
        cmd+=("--disable-cluster-pages")
    else
        cmd+=("--enable-cluster-pages")
    fi

    if [[ "${ENABLE_CLUSTER_MONITOR,,}" =~ ^(0|false|no|off)$ ]]; then
        cmd+=("--disable-cluster-monitor")
    else
        cmd+=("--enable-cluster-monitor")
    fi

    if [[ -n "$CLUSTER_MONITOR_INTERVAL" ]]; then
        cmd+=("--cluster-monitor-interval" "$CLUSTER_MONITOR_INTERVAL")
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

    # Cleanup existing processes
    cleanup_existing

    # Build and run command
    local cmd
    cmd=$(build_cmd)

    echo "[run] Starting dashboard on ${HOST}:${PORT}"
    [[ -n "$URL_PREFIX" ]] && echo "[run] URL prefix: ${URL_PREFIX}"
    echo "[run] Data directory: ${DATA_DIR}"
    echo "[run] Command: ${cmd}"
    echo "============================================"

    # Export data directory for the server
    export HPC_STATUS_DATA_DIR="$DATA_DIR"

    # Run the server
    exec $cmd
}

# Run main
main "$@"
