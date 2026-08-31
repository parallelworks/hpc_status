"""The deployment workflows must stay in step with each other.

There are three, one per deployment: workflow.yaml (generic),
yamls/hsp.yaml (HPCMP, which is what activate.hpc.mil runs) and
yamls/rdhpcs.yaml (NOAA). They are registered separately on separate
platforms, so a fix applied to one is invisible in the others — which is
how the endpoint upgrade landed in workflow.yaml while the platform that
mattered went on running the old sessions/update-session version of
yamls/hsp.yaml.

These tests pin what all three must share, and leave the per-deployment
defaults alone.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = [
    REPO / "workflow.yaml",
    REPO / "yamls" / "hsp.yaml",
    REPO / "yamls" / "rdhpcs.yaml",
]


def load(path: Path) -> dict:
    """Parse a workflow, working around PyYAML being YAML 1.1.

    A bare ``on:`` key loads as boolean True there; ACTIVATE reads it as
    the string it looks like.
    """
    document = yaml.safe_load(path.read_text())
    if True in document:
        document["on"] = document.pop(True)
    return document


def jobs_of(document: dict) -> dict:
    jobs = document["jobs"]
    assert len(jobs) == 1, (
        "one job: an empty ssh.remoteHost already runs it locally, so a "
        "second job for the workspace only raced the first"
    )
    return jobs


def step_of(document: dict) -> dict:
    (job,) = jobs_of(document).values()
    assert len(job["steps"]) == 1
    return job["steps"][0]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
class TestEveryWorkflow:
    def test_publishes_through_the_shared_script(self, path):
        run = step_of(load(path))["run"]
        assert "scripts/serve-endpoint.sh" in run, (
            "the endpoint logic lives in one script so the three cannot drift"
        )

    def test_does_not_register_a_session_by_hand(self, path):
        """update-session left dashboards running after the job ended."""
        document = load(path)
        assert "sessions" not in document, "pw endpoints owns the session now"
        for job in jobs_of(document).values():
            for step in job["steps"]:
                # Comments still describe the old mechanism, so read the
                # parsed steps rather than the file text.
                assert "update-session" not in str(step.get("uses", ""))
                assert "retry" not in step, "no more polling for the server"
                assert "curl --fail" not in step.get("run", "")

    def test_does_not_pin_the_port(self, path):
        """8080 is Grafana on an ACTIVATE workspace; let the CLI choose."""
        step = step_of(load(path))
        assert "PORT" not in step["env"], (
            "PORT must come from `pw endpoints run`, which tunnels to the "
            "port it assigned"
        )
        port_input = load(path)["on"]["execute"]["inputs"]["settings"]["items"]["port"]
        assert port_input["default"] == 0, "0 means: let the CLI pick a free port"

    def test_can_find_its_source_either_way(self, path):
        """Some platform versions check the repo out for the run; some don't."""
        run = step_of(load(path))["run"]
        assert "git clone" in run and "scripts/serve-endpoint.sh" in run
        env = step_of(load(path))["env"]
        assert "REPO_URL" in env and "REPO_BRANCH" in env

    def test_binds_loopback_only(self, path):
        """The tunnel dials localhost; nothing needs the node's address."""
        assert step_of(load(path))["env"]["HOST"] == "127.0.0.1"

    def test_offers_a_stable_public_address(self, path):
        """Without one the platform assigns a new random host every run."""
        items = load(path)["on"]["execute"]["inputs"]["settings"]["items"]
        assert items["subdomain"]["default"] == "", "blank derives status-<user>"
        assert "ENDPOINT_SUBDOMAIN" in step_of(load(path))["env"]


class TestWorkflowsAgree:
    def test_the_bootstrap_is_identical_everywhere(self):
        """Byte-identical, so a fix cannot land in one file and not another."""
        bodies = {}
        for path in WORKFLOWS:
            run = step_of(load(path))["run"]
            # Everything from locating the source onwards is shared; the
            # echo lines above it name the deployment.
            bodies[path.name] = run[run.index("# Locate the source.") :]
        distinct = set(bodies.values())
        assert len(distinct) == 1, (
            f"the bootstrap has drifted between {sorted(bodies)}"
        )

    def test_each_deployment_keeps_its_own_defaults(self):
        """Shared logic, not shared configuration."""
        platforms = {
            path.name: load(path)["on"]["execute"]["inputs"]["platform"]["default"]
            for path in WORKFLOWS
        }
        assert platforms == {
            # The generic one is what a marketplace launch resolves to, so
            # it works out where it is rather than assuming.
            "workflow.yaml": "auto",
            "hsp.yaml": "hpcmp",
            "rdhpcs.yaml": "noaa",
        }

    def test_the_noaa_workflow_pins_its_identity(self):
        """Without this, the collector queries whatever context is current."""
        env = step_of(load(REPO / "yamls" / "rdhpcs.yaml"))["env"]
        assert env["PW_CONTEXT"] == "noaa"


class TestServeEndpointScript:
    SCRIPT = REPO / "scripts" / "serve-endpoint.sh"

    def test_is_executable(self):
        assert self.SCRIPT.stat().st_mode & 0o111, (
            "the workflows invoke it with bash, but a human will not"
        )

    def test_passes_an_absolute_path_to_the_runner(self):
        """`pw endpoints run` fork/execs directly — a relative path is ENOENT."""
        source = self.SCRIPT.read_text()
        assert 'exec pw endpoints run "${args[@]}" -- bash "${launcher}"' in source
        assert 'launcher="${PROJECT_ROOT}/scripts/run.sh"' in source

    def test_only_pins_a_port_when_asked(self):
        source = self.SCRIPT.read_text()
        assert re.search(r'PINNED_PORT.*-gt 0', source), (
            "0 or unset must leave the port to the CLI"
        )

    def test_explains_itself_when_pw_is_missing(self):
        source = self.SCRIPT.read_text()
        assert "not on PATH" in source and "user workspace" in source

    @pytest.mark.parametrize(
        "user,expected",
        [
            ("Matthew.Shaxted", "status-matthew-shaxted"),
            ("mshaxted", "status-mshaxted"),
            ("Foo_Bar.99", "status-foo-bar-99"),
            ("-leading-and-trailing-", "status-leading-and-trailing"),
        ],
    )
    def test_derives_a_valid_hostname_label_from_the_user(self, user, expected):
        """Subdomains are lowercase alphanumerics and hyphens, no more."""
        import os
        import subprocess

        result = subprocess.run(
            ["bash", str(self.SCRIPT), "--print-subdomain"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "PW_USER": user, "ENDPOINT_SUBDOMAIN": ""},
        )
        assert result.stdout.strip() == expected, result.stderr

    @pytest.mark.parametrize(
        "host,config",
        [
            ("activate.hpc.mil", "configs/config.hpcmp.yaml"),
            ("hpcmp.parallel.works", "configs/config.hpcmp.yaml"),
            ("noaa.parallel.works", "configs/config.noaa.yaml"),
            ("activate.parallel.works", "configs/config.yaml"),
            ("", "configs/config.yaml"),
        ],
    )
    def test_auto_resolves_the_config_from_the_platform(self, host, config):
        """A marketplace launch runs workflow.yaml, which cannot see the host.

        Without this, the listing called "HPCMP Status" on a DoD platform
        would come up as a generic deployment with no fleet at all.
        """
        source = self.SCRIPT.read_text()
        body = source[source.index('case "${PW_PLATFORM_HOST:-}"') :]
        body = body[: body.index("esac") + 4]
        import subprocess

        result = subprocess.run(
            ["bash", "-c", f'PW_PLATFORM_HOST={host!r}\nCONFIG_FILE=""\n{body}\n'
             'echo "${CONFIG_FILE}"'],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.stdout.strip() == config, result.stderr

    def test_detached_mode_records_a_pid_and_a_way_back(self):
        """Detaching gives up cancel-stops-it, so it must be stoppable."""
        source = self.SCRIPT.read_text()
        assert "setsid nohup pw endpoints run" in source, (
            "a new process group is what lets it outlive the run"
        )
        assert "endpoint.pid" in source and "stop-endpoint.sh" in source
        assert "Endpoint live at" in source, (
            "detached mode must confirm the URL rather than report success blindly"
        )

    def test_detached_mode_replaces_rather_than_stacks(self):
        """Two of the same endpoint cannot coexist, so start means restart."""
        source = self.SCRIPT.read_text()
        assert "Clearing any previous instance" in source

    def test_falls_back_when_the_subdomain_is_refused(self):
        """A platform with no sessions domain must still serve the dashboard."""
        source = self.SCRIPT.read_text()
        assert "SECONDS - started > 20" in source, (
            "only an immediate failure is a subdomain problem; a long-running "
            "session that dies must not be silently restarted elsewhere"
        )
        assert "pw subdomains reserve" in source, "say how to claim the name"


class TestStopScript:
    SCRIPT = REPO / "scripts" / "stop-endpoint.sh"

    def test_exists_and_is_executable(self):
        assert self.SCRIPT.exists(), "detached mode is unusable without a way back"
        assert self.SCRIPT.stat().st_mode & 0o111

    def test_deletes_the_session_before_reaching_for_signals(self):
        """The session is the handle.

        `pw endpoints run` watches its own session and shuts down when it
        is deleted — "Endpoint ... was deleted; shutting down" — taking
        the dashboard and its workers with it. That is also the only path
        available from the platform UI, where nobody has a shell on the
        node, so it must come first.
        """
        source = self.SCRIPT.read_text()
        assert source.index("pw endpoints delete") < source.index("kill -TERM"), (
            "signalling first throws away the graceful shutdown the CLI does"
        )

    def test_signals_the_group_only_as_a_fallback(self):
        """A session deleted while the tunnel was disconnected leaves it."""
        source = self.SCRIPT.read_text()
        assert 'kill -TERM -- "-${pid}"' in source, "the group, not just the tunnel"
        assert 'kill -KILL -- "-${pid}"' in source, "escalate if TERM is ignored"
        assert "Waiting for pid" in source, "give the CLI time to notice"

    def test_says_something_useful_when_nothing_is_detached(self, tmp_path):
        import subprocess

        env = {**__import__("os").environ, "HPC_STATUS_DATA_DIR": str(tmp_path)}
        # Keep the pw CLI off PATH: with no pidfile and no reachable
        # control plane the script must still answer sensibly, and a unit
        # test must not depend on the network being in a good mood.
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        # No pidfile and no session is not an error — it is the answer.
        assert "No session named" in result.stdout
        assert "Stopped." in result.stdout


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
class TestStartIsTheOnlyAction:
    """Launching is the whole interface, and it restarts what is running.

    Two launches of the same endpoint name do not coexist — the platform
    hands the name to the newcomer and the incumbent logs "was replaced by
    another process; shutting down". Relying on that made a relaunch look
    like the dashboard dying at random, so the script stops the previous
    instance first, deliberately and in order.
    """

    def test_there_is_no_action_input(self, path):
        inputs = load(path)["on"]["execute"]["inputs"]
        assert "action" not in inputs, "starting is the only thing a run does"

    def test_the_step_does_not_branch(self, path):
        run = step_of(load(path))["run"]
        assert 'ACTION' not in run
        assert "stop-endpoint.sh" not in run, (
            "stopping is done by deleting the session, or by the script directly"
        )

    def test_it_keeps_serving_after_the_run_by_default(self, path):
        """Detached is viable again now that the workspace key is used.

        A run's injected credential is revoked seconds after the run
        completes, which is why this briefly defaulted to supervised. The
        launcher now adopts the workspace key from
        /etc/profile.d/parallelworks-env.sh, which outlives the run — so
        the run can finish without the fleet going coreless.
        """
        detach = load(path)["on"]["execute"]["inputs"]["settings"]["items"]["detach"]
        assert detach["default"] is True


class TestRestartOnStart:
    SCRIPT = REPO / "scripts" / "serve-endpoint.sh"

    def test_a_previous_instance_is_stopped_first(self):
        source = self.SCRIPT.read_text()
        assert "Clearing any previous instance" in source
        assert "stop-endpoint.sh" in source

    def test_it_does_not_depend_on_the_pidfile_being_there(self):
        """A session can outlive its pidfile; that was the racing case."""
        source = self.SCRIPT.read_text()
        clearing = source[source.index("Clearing any previous instance") :]
        clearing = clearing[: clearing.index("setsid")]
        assert "kill -0" not in clearing, (
            "the stop must run unconditionally, not only when a live pid is "
            "recorded"
        )

    def test_the_log_is_truncated_before_starting(self):
        """The readiness check greps this log; a stale hit is a false pass."""
        source = self.SCRIPT.read_text()
        assert ': > "${logfile}"' in source
        assert source.index(': > "${logfile}"') < source.index("setsid nohup")

    def test_no_url_is_a_failure_not_a_success(self):
        source = self.SCRIPT.read_text()
        assert "No URL after 2 minutes" in source, (
            "a dashboard that never announced a URL must not report success"
        )

    def test_it_waits_for_the_dashboard_not_just_the_tunnel(self):
        """A live endpoint means the tunnel registered, nothing more.

        The dashboard still has to install its dependencies and bind, and
        a visitor in that window gets a refused connection from a run that
        already reported success.
        """
        source = self.SCRIPT.read_text()
        assert "serving localhost:" in source, "read the assigned port from the log"
        assert "Dashboard answering on port" in source
        assert "Nothing is listening on" in source, (
            "a port that never opens is a failure, not a slow start"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
class TestWhereItRuns:
    """The dashboard can run somewhere that outlives the workspace.

    Everything ran on the user workspace, with no way to choose — and a
    workspace recycle takes the dashboard with it, which is how it was
    found dead one morning mid-sweep.
    """

    def test_a_host_can_be_chosen(self, path):
        host = load(path)["on"]["execute"]["inputs"]["host"]
        assert host["type"] == "compute-resources"
        assert host["optional"] is True, "blank must keep today's behaviour"
        assert host["include-workspace"] is True, (
            "the workspace is a legitimate choice, not only the fallback"
        )

    def test_the_host_targets_the_single_job(self, path):
        """Blank runs it here; a chosen host runs it there.

        The platform resolves an empty remoteHost itself — "ssh.remoteHost
        is empty; running this step on localhost" — so no branching is
        needed. A first attempt gated two jobs on `if` instead: both ran
        anyway, and they collided cloning the same checkout.
        """
        (job,) = jobs_of(load(path)).values()
        assert job["ssh"] == {"remoteHost": "${{ inputs.host.ip }}"}
        assert "if" not in job

    def test_no_job_is_gated_on_the_picker(self, path):
        document = load(path)
        for job in jobs_of(document).values():
            assert "if" not in job, "gating on inputs.host did not work"


class TestPreflight:
    """A cluster is not guaranteed to have what the workspace has."""

    SCRIPT = (REPO / "scripts" / "serve-endpoint.sh").read_text()

    def test_it_names_the_host_that_is_missing_something(self):
        assert "preflight_host" in self.SCRIPT
        assert "is not on PATH on ${preflight_host}" in self.SCRIPT

    def test_unauthenticated_is_its_own_message(self):
        """"pw exists" and "pw works" fail for different reasons."""
        assert "is not authenticated." in self.SCRIPT

    def test_both_point_back_at_the_workspace(self):
        """Say how to get back to the default that works."""
        assert self.SCRIPT.count("Where To") >= 2
        assert self.SCRIPT.count("user workspace") >= 2
