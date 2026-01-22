"""Tests for data collectors."""

import pytest
from unittest.mock import patch, MagicMock
from src.collectors.hpcmp import HPCMPCollector
from src.collectors.pw_cluster import PWClusterCollector


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

        # Should only include 'on' and 'existing' clusters
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
