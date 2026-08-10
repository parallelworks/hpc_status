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


def step_of(document: dict) -> dict:
    jobs = document["jobs"]
    assert len(jobs) == 1, "one job: publish the dashboard as an endpoint"
    (job,) = jobs.values()
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
        for job in document["jobs"].values():
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
        assert "not on PATH" in source and "scripts/run.sh directly" in source

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

    def test_detached_mode_refuses_to_stack(self):
        source = self.SCRIPT.read_text()
        assert "Already serving" in source, (
            "a second detached start would leave an unreachable dashboard behind"
        )

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

    def test_kills_the_group_not_just_the_tunnel(self):
        """Killing `pw endpoints run` alone can orphan the dashboard."""
        source = self.SCRIPT.read_text()
        assert 'kill -TERM -- "-${pid}"' in source
        assert 'kill -KILL -- "-${pid}"' in source, "escalate if TERM is ignored"

    def test_cleans_up_a_stale_pidfile(self):
        source = self.SCRIPT.read_text()
        assert "stale pidfile" in source

    def test_deletes_a_session_left_behind(self):
        """A killed tunnel never gets to delete its own session."""
        source = self.SCRIPT.read_text()
        assert "pw endpoints delete" in source

    def test_says_something_useful_when_nothing_is_detached(self, tmp_path):
        import subprocess

        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**__import__("os").environ, "HPC_STATUS_DATA_DIR": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert "nothing detached to stop" in result.stdout


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
class TestStopAction:
    """A detached dashboard needs a way back from the platform UI.

    Cancelling the run no longer stops it, and nobody has a shell on the
    workspace, so the workflow itself has to be able to stop one.
    """

    def test_offers_a_stop_action(self, path):
        action = load(path)["on"]["execute"]["inputs"]["action"]
        assert action["default"] == "start"
        assert {option["value"] for option in action["options"]} == {"start", "stop"}

    def test_stop_runs_the_stop_script(self, path):
        run = step_of(load(path))["run"]
        assert 'if [ "${ACTION}" = "stop" ]' in run
        assert "scripts/stop-endpoint.sh" in run

    def test_stop_happens_after_the_source_is_located(self, path):
        """The stop script lives in the checkout, so find it first."""
        run = step_of(load(path))["run"]
        assert run.index("# Locate the source.") < run.index('= "stop" ]')
