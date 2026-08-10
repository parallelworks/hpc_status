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
#   ENDPOINT_NAME       name for the session (default: hpc-status)
#   ENDPOINT_SUBDOMAIN  public hostname label (default: status-<user>)
#   PINNED_PORT         local port, or 0/unset to let the CLI choose
#   Everything scripts/run.sh understands (CONFIG_FILE, DEFAULT_THEME, ...)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENDPOINT_NAME="${ENDPOINT_NAME:-hpc-status}"
PINNED_PORT="${PINNED_PORT:-0}"

# Without a subdomain the platform assigns a random one per session, so
# the dashboard moves — healthy-dingo today, expert-sponge tomorrow. A
# name derived from the user is stable across runs and unique between
# people on the same platform. `pw subdomains reserve <name>` makes the
# claim permanent; this only asks for it.
if [[ -z "${ENDPOINT_SUBDOMAIN:-}" ]]; then
    # PW_USER inside a workflow run, the login name otherwise.
    who="${PW_USER:-${USER:-$(id -un)}}"
    # Hostname labels are lowercase alphanumerics and hyphens, so
    # Matthew.Shaxted becomes matthew-shaxted.
    who="$(printf '%s' "${who}" | tr '[:upper:]' '[:lower:]' |
           sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
    ENDPOINT_SUBDOMAIN="status${who:+-${who}}"
fi

# Lets the tests check the derived name without publishing anything.
if [[ "${1:-}" == "--print-subdomain" ]]; then
    echo "${ENDPOINT_SUBDOMAIN}"
    exit 0
fi

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
echo "[endpoint] Requesting subdomain '${ENDPOINT_SUBDOMAIN}'"

started=${SECONDS}
set +e
pw endpoints run "${args[@]}" --subdomain "${ENDPOINT_SUBDOMAIN}" -- bash "${launcher}"
status=$?
set -e

if [[ ${status} -eq 0 ]]; then
    exit 0
fi

# A platform with no sessions domain, or a subdomain somebody else holds,
# fails immediately. Anything that ran for a while failed for its own
# reasons and must not be silently restarted somewhere else.
if (( SECONDS - started > 20 )); then
    exit ${status}
fi

echo "[endpoint] Could not serve at '${ENDPOINT_SUBDOMAIN}' — falling back" >&2
echo "[endpoint] to a platform-assigned address. Run" >&2
echo "[endpoint]   pw subdomains reserve ${ENDPOINT_SUBDOMAIN}" >&2
echo "[endpoint] to claim it, or set ENDPOINT_SUBDOMAIN to another name." >&2
exec pw endpoints run "${args[@]}" -- bash "${launcher}"
