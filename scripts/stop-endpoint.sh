#!/usr/bin/env bash
#
# Stop a detached dashboard endpoint.
#
# In the supervised mode the workflow run owns the dashboard and
# cancelling the run stops it. A detached one outlives the run that
# started it, so this is the way back: kill the process group `pw
# endpoints run` was started in, which takes the tunnel, the dashboard and
# its workers with it, and delete the session so the name is free again.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

STATE_DIR="${HPC_STATUS_DATA_DIR:-${HOME}/.hpc_status}"
PIDFILE="${STATE_DIR}/endpoint.pid"
ENDPOINT_NAME="${ENDPOINT_NAME:-hpc-status}"

if [[ ! -f "${PIDFILE}" ]]; then
    echo "[endpoint] No pidfile at ${PIDFILE} — nothing detached to stop."
    echo "[endpoint] Running sessions: "
    pw endpoints list 2>/dev/null || true
    exit 0
fi

pid="$(cat "${PIDFILE}")"
if ! kill -0 "${pid}" 2>/dev/null; then
    echo "[endpoint] pid ${pid} is not running; clearing stale pidfile."
    rm -f "${PIDFILE}"
else
    # setsid made the endpoint a process-group leader, so a negative pid
    # reaches the dashboard and its workers too, not just the tunnel.
    echo "[endpoint] Stopping pid ${pid} and its process group"
    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null

    for _ in $(seq 1 15); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 1
    done

    if kill -0 "${pid}" 2>/dev/null; then
        echo "[endpoint] Still alive after 15s; sending KILL"
        kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null
    fi
    rm -f "${PIDFILE}"
fi

# `pw endpoints run` deletes the session on a clean exit; after a KILL it
# may not have got there.
if pw endpoints list 2>/dev/null | grep -q "^${ENDPOINT_NAME}[[:space:]]"; then
    echo "[endpoint] Deleting leftover session '${ENDPOINT_NAME}'"
    pw endpoints delete "${ENDPOINT_NAME}" 2>&1 | sed 's/^/[endpoint] /' || true
fi

echo "[endpoint] Stopped."
