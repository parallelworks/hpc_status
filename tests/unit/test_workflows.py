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
            "workflow.yaml": "generic",
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

    def test_falls_back_when_the_subdomain_is_refused(self):
        """A platform with no sessions domain must still serve the dashboard."""
        source = self.SCRIPT.read_text()
        assert "SECONDS - started > 20" in source, (
            "only an immediate failure is a subdomain problem; a long-running "
            "session that dies must not be silently restarted elsewhere"
        )
        assert "pw subdomains reserve" in source, "say how to claim the name"
