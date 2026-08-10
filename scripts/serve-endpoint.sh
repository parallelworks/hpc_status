#!/usr/bin/env bash
#
# Publish the dashboard as an ACTIVATE endpoint session.
#
# `pw endpoints run` assigns a free local port, exports it as PORT (which
# scripts/run.sh reads), dials out to register a reverse tunnel so the node
# needs no inbound access, and tears the whole process tree down when it
# exits — so cancelling a run actually stops the dashboard.
#
# This lives in the repo rather than in the workflow YAMLs because there
# are three of them (workflow.yaml, yamls/hsp.yaml, yamls/rdhpcs.yaml) for
# three deployments, and the last time they drifted, the fix landed in one
# and the platform that mattered ran another.
#
# Environment:
#   ENDPOINT_NAME  name for the session (default: hpc-status)
#   PINNED_PORT    local port, or 0/unset to let the CLI choose
#   Everything scripts/run.sh understands (CONFIG_FILE, DEFAULT_THEME, ...)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENDPOINT_NAME="${ENDPOINT_NAME:-hpc-status}"
PINNED_PORT="${PINNED_PORT:-0}"

if ! command -v pw >/dev/null 2>&1; then
    echo "The pw CLI is not on PATH; it is required to publish the dashboard" >&2
    echo "as an endpoint session. Run ./scripts/run.sh directly to serve it" >&2
    echo "on this machine only." >&2
    exit 1
fi

# `pw endpoints run` fork/execs the command rather than going through a
# shell, so it needs an absolute path — and `bash <script>` rather than the
# script itself, so a checkout that lost the executable bit still starts.
launcher="${PROJECT_ROOT}/scripts/run.sh"
if [[ ! -f "${launcher}" ]]; then
    echo "Cannot find ${launcher}" >&2
    exit 1
fi

args=(--name "${ENDPOINT_NAME}")
if [[ "${PINNED_PORT}" =~ ^[0-9]+$ ]] && [[ "${PINNED_PORT}" -gt 0 ]]; then
    # Only pin a port when asked: the assigned one is what the tunnel
    # watches, and 8080 is Grafana on an ACTIVATE workspace.
    args+=(--port "${PINNED_PORT}")
fi

echo "[endpoint] Publishing as '${ENDPOINT_NAME}' from ${PROJECT_ROOT}"
exec pw endpoints run "${args[@]}" -- bash "${launcher}"
