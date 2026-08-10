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

# A marketplace launch runs workflow.yaml, whose platform input defaults
# to "auto" and leaves CONFIG_FILE empty, because a `${{ }}` expression
# cannot look at the host it is running on. The listing that says "HPCMP
# Status" should not come up configured for a generic deployment.
if [[ -z "${CONFIG_FILE:-}" ]]; then
    case "${PW_PLATFORM_HOST:-}" in
        *hpc.mil*|*hpcmp*) CONFIG_FILE="configs/config.hpcmp.yaml" ;;
        *noaa*|*rdhpcs*)   CONFIG_FILE="configs/config.noaa.yaml" ;;
        *)                 CONFIG_FILE="configs/config.yaml" ;;
    esac
    export CONFIG_FILE
    echo "[endpoint] Platform ${PW_PLATFORM_HOST:-unknown} -> ${CONFIG_FILE}"
fi

echo "[endpoint] Publishing as '${ENDPOINT_NAME}' from ${PROJECT_ROOT}"
echo "[endpoint] Requesting subdomain '${ENDPOINT_SUBDOMAIN}'"

# Detached mode lets the workflow run finish while the dashboard keeps
# serving. It costs the thing that makes the supervised mode good:
# cancelling the run no longer stops anything, so stop-endpoint.sh and a
# pidfile are the only way back.
if [[ "${DETACH:-0}" =~ ^(1|true|yes)$ ]]; then
    state_dir="${HPC_STATUS_DATA_DIR:-${HOME}/.hpc_status}"
    mkdir -p "${state_dir}"
    pidfile="${state_dir}/endpoint.pid"
    logfile="${state_dir}/endpoint.log"

    # Starting is a restart when something is already serving.
    #
    # Two launches of the same endpoint name do not coexist: the platform
    # hands the name to the newcomer and the incumbent logs "was replaced
    # by another process; shutting down". Left to chance that reads as the
    # dashboard dying at random, so do it deliberately and in order —
    # stop, wait, start — rather than racing.
    # Unconditionally, not only when the pidfile says so: the session can
    # outlive the pidfile (a restarted workspace, a run from a different
    # job directory), and that is exactly the case that used to race.
    # Stopping nothing is a no-op that costs one listing.
    echo "[endpoint] Clearing any previous instance before starting"
    ENDPOINT_NAME="${ENDPOINT_NAME}" bash "${PROJECT_ROOT}/scripts/stop-endpoint.sh" \
        2>&1 | sed 's/^\[endpoint\] /  /' || true

    # Start from an empty log. It is opened in append mode so a restart
    # keeps nothing from last time, and the readiness check below greps
    # for a line this run must produce — matching the previous run's copy
    # of it would report success without ever confirming anything.
    : > "${logfile}"

    # A workspace recycle kills the endpoint without letting it
    # deregister, and the platform keeps listing the dead session as
    # "running" — squatting on the name and subdomain while serving
    # nothing. Reaching this line means no live process of ours exists
    # (the pidfile check above), so a session under this name is stale
    # by definition; delete it so the fresh one takes its place.
    if pw endpoints list 2>/dev/null | grep -q "^${ENDPOINT_NAME}[[:space:]]"; then
        echo "[endpoint] Removing stale session '${ENDPOINT_NAME}' (no live local process)"
        pw endpoints delete "${ENDPOINT_NAME}" 2>&1 | sed 's/^/[endpoint] /' || true
    fi

    # setsid detaches from the job's process group, which is what lets it
    # outlive the step; the runner does not reap it.
    setsid nohup pw endpoints run "${args[@]}" \
        --subdomain "${ENDPOINT_SUBDOMAIN}" -- bash "${launcher}" \
        >>"${logfile}" 2>&1 </dev/null &
    endpoint_pid=$!
    echo "${endpoint_pid}" > "${pidfile}"

    # Wait for the URL rather than reporting success blindly. The log was
    # truncated above, so this line can only be this run's.
    live=""
    for _ in $(seq 1 60); do
        if grep -qm1 "Endpoint live at" "${logfile}" 2>/dev/null; then
            live="yes"
            break
        fi
        if ! kill -0 "${endpoint_pid}" 2>/dev/null; then
            echo "[endpoint] The endpoint exited during startup:" >&2
            tail -20 "${logfile}" >&2
            rm -f "${pidfile}"
            exit 1
        fi
        sleep 2
    done

    if [[ -z "${live}" ]]; then
        # Still running but never announced a URL. Reporting success here
        # is how a dead dashboard looks healthy.
        echo "[endpoint] No URL after 2 minutes; the endpoint is not serving." >&2
        tail -20 "${logfile}" >&2
        exit 1
    fi

    # A live endpoint means the tunnel registered, not that anything is
    # answering behind it — the dashboard still has to install its deps
    # and collect. Wait for the port the CLI assigned to serve a page, so
    # "started" means a visitor gets the dashboard rather than a refused
    # connection.
    local_port="$(sed -n 's/.*serving localhost:\([0-9]\{1,\}\).*/\1/p' \
        "${logfile}" | head -1)"
    if [[ -n "${local_port}" ]]; then
        for _ in $(seq 1 60); do
            if (exec 3<>"/dev/tcp/127.0.0.1/${local_port}") 2>/dev/null; then
                exec 3>&- 2>/dev/null || true
                break
            fi
            if ! kill -0 "${endpoint_pid}" 2>/dev/null; then
                echo "[endpoint] The dashboard exited before it served:" >&2
                tail -20 "${logfile}" >&2
                rm -f "${pidfile}"
                exit 1
            fi
            sleep 2
        done
        if ! (exec 3<>"/dev/tcp/127.0.0.1/${local_port}") 2>/dev/null; then
            echo "[endpoint] Nothing is listening on ${local_port} after 2 minutes." >&2
            tail -20 "${logfile}" >&2
            exit 1
        fi
        exec 3>&- 2>/dev/null || true
        echo "[endpoint] Dashboard answering on port ${local_port}"
    fi

    echo "[endpoint] Detached (pid ${endpoint_pid}), logging to ${logfile}"
    grep -m1 "Endpoint live at" "${logfile}" || true
    echo "[endpoint] Stop it by deleting the session, or with"
    echo "[endpoint]   ${PROJECT_ROOT}/scripts/stop-endpoint.sh"
    exit 0
fi

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
