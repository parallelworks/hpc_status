#!/usr/bin/env bash
#
# Bootstrap dependencies and run the HPC dashboard server.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
URL_PREFIX="${URL_PREFIX:-}"
DEFAULT_THEME="${DEFAULT_THEME:-light}"
ENABLE_CLUSTER_PAGES="${ENABLE_CLUSTER_PAGES:-1}"
ENABLE_CLUSTER_MONITOR="${ENABLE_CLUSTER_MONITOR:-${ENABLE_CLUSTER_PAGES}}"
CLUSTER_MONITOR_INTERVAL="${CLUSTER_MONITOR_INTERVAL:-}"

cd "${SCRIPT_DIR}"

echo "[run_dashboard] Installing Python dependencies..."
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install -r requirements.txt

cmd=("${PYTHON_BIN}" "dashboard_server.py" "--host" "${HOST}" "--port" "${PORT}" "--default-theme" "${DEFAULT_THEME}")
if [[ -n "${URL_PREFIX}" ]]; then
  cmd+=("--url-prefix" "${URL_PREFIX}")
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
if [[ -n "${CLUSTER_MONITOR_INTERVAL}" ]]; then
  cmd+=("--cluster-monitor-interval" "${CLUSTER_MONITOR_INTERVAL}")
fi

echo "[run_dashboard] Starting dashboard on ${HOST}:${PORT} ${URL_PREFIX:+(prefix: ${URL_PREFIX})} (default theme: ${DEFAULT_THEME})"
echo "${cmd[@]}"
exec "${cmd[@]}"
