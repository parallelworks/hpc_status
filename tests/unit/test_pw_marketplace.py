"""Reading the platform's compute catalog.

The catalog answers a question no status source does — what is this
machine for — and surfaces systems that are real but unpublished. It is
also entirely optional: a deployment without a marketplace still has a
fleet page, so nothing here may raise.
"""

import json
import subprocess
from unittest.mock import patch

from src.collectors.pw_marketplace import PWMarketplaceCollector


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["pw"], returncode=returncode, stdout=stdout, stderr=stderr
    )


LISTINGS = [
    {
        "slug": "coral",
        "name": "Coral",
        "type": "compute",
        "subtype": "existing",
        "description": "Heterogeneous x86_64 and ARM cluster.",
        "tags": ["slurm", "mhpcc", "hpc"],
        "publisher": "HPCMP",
    },
    {"slug": "hpcmp_status", "name": "HPC Status", "type": "workflow"},
    {"slug": "nostatus", "name": "No Status", "type": "compute", "subtype": "existing"},
]


class TestParsing:
    def test_only_compute_listings_become_systems(self):
        with patch("subprocess.run", return_value=completed(json.dumps(LISTINGS))):
            rows = PWMarketplaceCollector().listings()
        assert [row["slug"] for row in rows] == ["coral", "nostatus"], (
            "a workflow listing is not a machine"
        )

    def test_it_keeps_what_the_fleet_page_shows(self):
        with patch("subprocess.run", return_value=completed(json.dumps(LISTINGS))):
            coral = PWMarketplaceCollector().listings()[0]
        assert coral["name"] == "Coral"
        assert coral["description"].startswith("Heterogeneous")
        assert coral["tags"] == ["slurm", "mhpcc", "hpc"]
        assert coral["subtype"] == "existing"

    def test_a_scheduler_tag_is_read(self):
        """Tags are free-form; only a known scheduler vocabulary is read."""
        with patch("subprocess.run", return_value=completed(json.dumps(LISTINGS))):
            rows = PWMarketplaceCollector().listings()
        assert rows[0]["scheduler"] == "SLURM"
        assert rows[1]["scheduler"] is None

    def test_a_missing_description_is_none_not_empty(self):
        with patch("subprocess.run", return_value=completed(json.dumps(LISTINGS))):
            rows = PWMarketplaceCollector().listings()
        assert rows[1]["description"] is None

    def test_an_envelope_object_is_unwrapped(self):
        payload = json.dumps({"items": LISTINGS})
        with patch("subprocess.run", return_value=completed(payload)):
            assert len(PWMarketplaceCollector().listings()) == 2


class TestFailureIsNotFatal:
    """No marketplace is a missing nicety, not a broken dashboard."""

    def test_a_failed_command_yields_no_listings(self):
        with patch("subprocess.run", return_value=completed(returncode=1, stderr="nope")):
            assert PWMarketplaceCollector().listings() == []

    def test_unreadable_output_yields_no_listings(self):
        with patch("subprocess.run", return_value=completed("not json at all")):
            assert PWMarketplaceCollector().listings() == []

    def test_a_missing_pw_binary_yields_no_listings(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("pw")):
            assert PWMarketplaceCollector().listings() == []

    def test_a_timeout_yields_no_listings(self):
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pw", timeout=30)
        ):
            assert PWMarketplaceCollector().listings() == []

    def test_junk_entries_are_skipped(self):
        payload = json.dumps([{"type": "compute"}, "not-a-dict", LISTINGS[0]])
        with patch("subprocess.run", return_value=completed(payload)):
            rows = PWMarketplaceCollector().listings()
        assert [row["slug"] for row in rows] == ["coral"]


class TestCaching:
    def test_the_catalog_is_not_refetched_every_poll(self):
        """Listings change when somebody publishes; the fleet polls constantly."""
        collector = PWMarketplaceCollector()
        with patch(
            "subprocess.run", return_value=completed(json.dumps(LISTINGS))
        ) as run:
            collector.listings()
            collector.listings()
        assert run.call_count == 1

    def test_a_forced_read_bypasses_the_cache(self):
        collector = PWMarketplaceCollector()
        with patch(
            "subprocess.run", return_value=completed(json.dumps(LISTINGS))
        ) as run:
            collector.listings()
            collector.listings(force=True)
        assert run.call_count == 2


class TestContext:
    def test_a_pinned_context_is_passed_through(self):
        collector = PWMarketplaceCollector(pw_context="user:me@activate.hpc.mil")
        with patch(
            "subprocess.run", return_value=completed(json.dumps(LISTINGS))
        ) as run:
            collector.listings()
        assert run.call_args[0][0][:3] == ["pw", "--context", "user:me@activate.hpc.mil"]
