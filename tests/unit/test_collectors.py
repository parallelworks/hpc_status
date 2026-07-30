"""Tests for data collectors."""

import pytest
from unittest.mock import patch, MagicMock
from src.collectors.hpcmp import HPCMPCollector
from src.collectors.pw_cluster import PWClusterCollector
from src.collectors.noaa import (
    NOAA_BRIEFING_SOURCES,
    NOAA_SYSTEM_ALIASES,
    resolve_briefing_slug,
)


class TestHPCMPCollector:
    def test_name_property(self):
        collector = HPCMPCollector()
        assert collector.name == "hpcmp"

    def test_display_name_property(self):
        collector = HPCMPCollector()
        assert collector.display_name == "HPCMP Fleet Status"

    def test_normalize_status_up(self):
        collector = HPCMPCollector()
        assert collector._normalize_status("Up") == "UP"
        assert collector._normalize_status("available") == "UP"
        assert collector._normalize_status("online") == "UP"
        assert collector._normalize_status("operational") == "UP"

    def test_normalize_status_down(self):
        collector = HPCMPCollector()
        assert collector._normalize_status("Down") == "DOWN"
        assert collector._normalize_status("offline") == "DOWN"
        assert collector._normalize_status("unavailable") == "DOWN"

    @pytest.mark.parametrize(
        "phrase,expected",
        [
            # The bug this guards: substring matching read "unavailable" as
            # UP ("available") and "upgrading" as UP ("up"), so a dead
            # system was reported healthy.
            ("unavailable", "DOWN"),
            ("Currently unavailable", "DOWN"),
            ("not available", "DOWN"),
            ("upgrading", "MAINTENANCE"),
            ("undergoing an upgrade", "MAINTENANCE"),
            ("backup in progress", "UNKNOWN"),
            ("supported", "UNKNOWN"),
            # Bad news wins: a machine down for maintenance is still down.
            ("Down for scheduled maintenance", "DOWN"),
            ("intermittent", "DEGRADED"),
            ("healthy", "UP"),
        ],
    )
    def test_normalize_status_matches_whole_words(self, phrase, expected):
        collector = HPCMPCollector()
        assert collector._normalize_status(phrase) == expected

    @pytest.mark.parametrize(
        "alt,expected",
        [
            ("Nautilus is currently Up.", "UP"),
            ("Raider is currently Down.", "DOWN"),
            ("Warhawk is currently Degraded.", "DEGRADED"),
            ("Jean is currently unavailable.", "DOWN"),
            ("Ruth is currently in Maintenance.", "MAINTENANCE"),
        ],
    )
    def test_parse_status_from_alt_text(self, alt, expected):
        collector = HPCMPCollector()
        assert collector._parse_status_from_alt(alt) == expected

    def test_normalize_status_degraded(self):
        collector = HPCMPCollector()
        assert collector._normalize_status("Degraded") == "DEGRADED"
        assert collector._normalize_status("limited") == "DEGRADED"
        assert collector._normalize_status("partial") == "DEGRADED"

    def test_normalize_status_maintenance(self):
        collector = HPCMPCollector()
        assert collector._normalize_status("Maintenance") == "MAINTENANCE"
        assert collector._normalize_status("outage window") == "MAINTENANCE"

    def test_normalize_status_unknown(self):
        collector = HPCMPCollector()
        assert collector._normalize_status("") == "UNKNOWN"
        assert collector._normalize_status("something else") == "UNKNOWN"

    def test_parse_system_from_alt(self):
        collector = HPCMPCollector()
        assert collector._parse_system_from_alt("Nautilus is currently Up.") == "Nautilus"
        assert collector._parse_system_from_alt("Jean is Degraded.") == "Jean"
        assert collector._parse_system_from_alt("") is None
        assert collector._parse_system_from_alt(None) is None

    def test_parse_status_from_alt(self):
        collector = HPCMPCollector()
        assert collector._parse_status_from_alt("Nautilus is currently Up.") == "UP"
        assert collector._parse_status_from_alt("System is Degraded") == "DEGRADED"
        assert collector._parse_status_from_alt("Down for maintenance") == "DOWN"

    def test_build_login(self):
        collector = HPCMPCollector()
        assert collector._build_login("Nautilus", "navy") == "nautilus.navydsrc.hpc.mil"
        assert collector._build_login("Raider", "afrl") == "raider.afrl.hpc.mil"
        assert collector._build_login("Onyx", "erdc") == "onyx.erdc.hpc.mil"
        assert collector._build_login("System", None) is None
        assert collector._build_login("", "navy") is None

    def test_guess_from_src(self):
        collector = HPCMPCollector()
        assert collector._guess_from_src("/images/up.png") == "UP"
        assert collector._guess_from_src("/images/down.gif") == "DOWN"
        assert collector._guess_from_src("/images/degraded.png") == "DEGRADED"
        assert collector._guess_from_src("/images/maint.png") == "MAINTENANCE"
        assert collector._guess_from_src("/images/unknown.png") is None


class TestPWClusterCollector:
    def test_name_property(self):
        collector = PWClusterCollector()
        assert collector.name == "pw_cluster"

    def test_display_name_property(self):
        collector = PWClusterCollector()
        assert collector.display_name == "PW Clusters"

    def test_parse_cluster_table(self, sample_pw_clusters_output):
        collector = PWClusterCollector()
        clusters = collector._parse_cluster_table(sample_pw_clusters_output)

        # Should only include active existing clusters
        assert len(clusters) == 2
        assert clusters[0]["uri"] == "pw://user/nautilus"
        assert clusters[0]["status"] == "active"
        assert clusters[1]["uri"] == "pw://user/jean"

    def test_parse_cluster_table_pipe_format(self, sample_pw_clusters_output_pipe):
        collector = PWClusterCollector()
        clusters = collector._parse_cluster_table(sample_pw_clusters_output_pipe)

        # Legacy pipe-delimited format should also work
        assert len(clusters) == 2
        assert clusters[0]["uri"] == "pw://user/nautilus"
        assert clusters[0]["status"] == "on"
        assert clusters[1]["uri"] == "pw://user/jean"

    def test_parse_usage_output(self, sample_usage_output):
        collector = PWClusterCollector()
        usage = collector._parse_usage_output(sample_usage_output)

        assert "systems" in usage
        assert len(usage["systems"]) == 2
        assert usage["systems"][0]["system"] == "nautilus"
        assert usage["systems"][0]["hours_allocated"] == 250000
        assert usage["systems"][0]["percent_remaining"] == 100.0

    def test_parse_queue_output_handles_both_column_layouts(self):
        """show_queues ships with and without the per-job cores column.

        Fixed positional indexes silently dropped every row on the shorter
        layout, so the parser reads the header when it recognizes it.
        """
        collector = PWClusterCollector()
        nine_column = """QUEUE INFORMATION:
Queue Name   Max Time    Max Jobs  Max Cores  Running  Pending  Cores Run  Cores Pend  Type
=========================================================================================
standard     24:00:00    -         -          4        0        384        0           Exe
gpu          12:00:00    -         -          1        2        64         128         GPU
"""
        ten_column = """QUEUE INFORMATION:
Queue Name  Max Time  Max Jobs  Max Cores  Max Cores Per Job  Running  Pending  Cores Run  Cores Pend  Type
==========================================================================================================
standard    24:00:00  -         -          1024               4        0        384        0           Exe
"""
        nine = collector._parse_queue_output(nine_column)["queues"]
        ten = collector._parse_queue_output(ten_column)["queues"]

        assert [q["queue_name"] for q in nine] == ["standard", "gpu"]
        assert nine[0]["cores_running"] == "384"
        assert nine[1]["cores_pending"] == "128"
        assert nine[0]["queue_type"] == "Exe"

        assert ten[0]["max_cores_per_job"] == "1024"
        assert ten[0]["cores_running"] == "384"

    def test_parse_queue_output_skips_unrecognized_headers(self):
        """An unknown layout yields nothing rather than misaligned fields."""
        collector = PWClusterCollector()
        output = """QUEUE INFORMATION:
Queue Name   Frobnicator   Running
==================================
standard     x             4
"""
        assert collector._parse_queue_output(output)["queues"] == []

    def test_parse_queue_output(self, sample_queue_output):
        collector = PWClusterCollector()
        queue_data = collector._parse_queue_output(sample_queue_output)

        assert "queues" in queue_data
        assert "nodes" in queue_data
        assert len(queue_data["queues"]) >= 1
        assert len(queue_data["nodes"]) >= 1

    @patch("subprocess.run")
    def test_is_available_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        collector = PWClusterCollector()
        assert collector.is_available() is True

    @patch("subprocess.run")
    def test_is_available_false(self, mock_run):
        mock_run.side_effect = Exception("pw not found")
        collector = PWClusterCollector()
        assert collector.is_available() is False


class TestResolveBriefingSlug:
    def test_exact_match_wins(self):
        for canonical in NOAA_BRIEFING_SOURCES.keys():
            assert resolve_briefing_slug(canonical) == canonical

    def test_substring_alias_resolves(self):
        # Renamed / short PW cluster names should still find their briefing.
        assert resolve_briefing_slug("ursa") == "noaaursacluster"
        assert resolve_briefing_slug("hera") == "noaaheracluster"
        assert resolve_briefing_slug("gaeac5prod") == "noaagaeac5cluster"
        assert resolve_briefing_slug("ppanlogin") == "noaappancluster"
        assert resolve_briefing_slug("mercurydm") == "noaamercurysystem"
        assert resolve_briefing_slug("noaacloudv3") == "gclusternoaav3"

    def test_unknown_slug_returns_none(self):
        assert resolve_briefing_slug("randomcluster") is None
        assert resolve_briefing_slug("") is None
        assert resolve_briefing_slug(None) is None

    def test_alias_table_covers_every_briefing_source(self):
        # Every canonical briefing key should be reachable via at least one
        # alias stem, otherwise a renamed cluster could silently lose its
        # briefing.
        canonical_values = set(NOAA_SYSTEM_ALIASES.values())
        assert canonical_values == set(NOAA_BRIEFING_SOURCES.keys())
