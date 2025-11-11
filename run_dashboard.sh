#!/usr/bin/env bash
#
# Bootstrap dependencies and run the HPC dashboard server.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
URL_PREFIX="${URL_PREFIX:-}"

cd "${SCRIPT_DIR}"

echo "[run_dashboard] Installing Python dependencies..."
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install -r requirements.txt

cmd=("${PYTHON_BIN}" "dashboard_server.py" "--host" "${HOST}" "--port" "${PORT}")
if [[ -n "${URL_PREFIX}" ]]; then
  cmd+=("--url-prefix" "${URL_PREFIX}")
fi

echo "[run_dashboard] Starting dashboard on ${HOST}:${PORT} ${URL_PREFIX:+(prefix: ${URL_PREFIX})}"
exec "${cmd[@]}"
