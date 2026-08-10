#!/usr/bin/env bash
#
# Stop a detached dashboard endpoint.
#
# The session is the handle. `pw endpoints run` watches its own session,
# and deleting it server-side makes the CLI shut down and take the
# dashboard and its workers with it:
#
#     Endpoint "hpc-status" was deleted; shutting down.
#
# So the first thing this does is what the ACTIVATE sessions UI does —
# delete the session — and only then falls back to signalling the process
# group, for a dashboard whose session went away without it noticing.
#
# In the supervised (non-detached) mode none of this is needed:
# cancelling the workflow run stops everything.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

STATE_DIR="${HPC_STATUS_DATA_DIR:-${HOME}/.hpc_status}"
PIDFILE="${STATE_DIR}/endpoint.pid"
ENDPOINT_NAME="${ENDPOINT_NAME:-hpc-status}"

pid=""
if [[ -f "${PIDFILE}" ]]; then
    pid="$(cat "${PIDFILE}")"
fi

# 1. Delete the session. This is the graceful path and the only one that
#    works from the platform UI, where nobody has a shell on the node.
if pw endpoints list 2>/dev/null | grep -q "^${ENDPOINT_NAME}[[:space:]]"; then
    echo "[endpoint] Deleting session '${ENDPOINT_NAME}'"
    pw endpoints delete "${ENDPOINT_NAME}" 2>&1 | sed 's/^/[endpoint] /'
else
    echo "[endpoint] No session named '${ENDPOINT_NAME}' is registered"
fi

# 2. Give the CLI a moment to notice and shut itself down.
if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "[endpoint] Waiting for pid ${pid} to shut down"
    for _ in $(seq 1 20); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 1
    done
fi

# 3. Only if it is still there — a session deleted while the tunnel was
#    disconnected, say — signal the group. setsid made the endpoint a
#    process-group leader, so a negative pid reaches the dashboard and
#    its workers too, not just the tunnel.
if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "[endpoint] Still running; stopping pid ${pid} and its process group"
    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null
    for _ in $(seq 1 15); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        echo "[endpoint] Ignored TERM; sending KILL"
        kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null
    fi
fi

if [[ -n "${pid}" ]]; then
    rm -f "${PIDFILE}"
fi

echo "[endpoint] Stopped."
