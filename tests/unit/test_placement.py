"""Tests for the job placement planner."""

import pytest

from src.insights.placement import (
    PlacementRequest,
    parse_walltime_hours,
    rank_placements,
)


def cluster(name, *, status="on", cores_total=1000, cores_running=0, gpus=0,
            hours_remaining=None, queues=()):
    entry = {
        "cluster_metadata": {"name": name, "status": status},
        "queue_data": {
            "cluster_totals": {
                "cores_total": cores_total,
                "cores_running": cores_running,
                "gpus_total": gpus,
            },
            "queues": list(queues),
        },
    }
    if hours_remaining is not None:
        entry["usage_data"] = {"systems": [{"hours_remaining": str(hours_remaining)}]}
    return entry


def queue(name="standard", *, walltime="24:00:00", running=0, pending=0, qtype=None,
          max_cores="-"):
    return {
        "queue_name": name,
        "max_walltime": walltime,
        "max_cores": max_cores,
        "cores_running": running,
        "cores_pending": pending,
        "queue_type": qtype,
    }


class TestParseWalltime:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("24:00:00", 24.0),
            ("01:30:00", 1.5),
            ("168:00:00", 168.0),
            ("7-00:00:00", 168.0),  # Slurm day-dash form
            ("2:00:00:00", 48.0),  # DD:HH:MM:SS
            ("12", 12.0),
            ("-", None),
            ("unlimited", None),
            ("", None),
            ("nonsense", None),
        ],
    )
    def test_formats(self, value, expected):
        assert parse_walltime_hours(value) == expected


class TestRanking:
    def test_prefers_the_less_contended_queue(self):
        clusters = [
            cluster("busy", cores_total=1000, cores_running=200,
                    queues=[queue(pending=800)]),
            cluster("quiet", cores_total=1000, cores_running=200,
                    queues=[queue(pending=0)]),
        ]
        result = rank_placements(clusters, PlacementRequest(cores=100, hours=2))
        assert [c["cluster"] for c in result["candidates"]] == ["quiet", "busy"]

    def test_walltime_limit_blocks_rather_than_ranks(self):
        clusters = [
            cluster("a", queues=[queue("debug", walltime="01:00:00"), queue("standard")])
        ]
        result = rank_placements(clusters, PlacementRequest(cores=10, hours=6))

        assert [c["queue"] for c in result["candidates"]] == ["standard"]
        blocked = result["blocked"][0]
        assert blocked["queue"] == "debug"
        assert "walltime limit is 1h" in blocked["blockers"][0]

    def test_job_larger_than_the_cluster_is_blocked(self):
        clusters = [cluster("small", cores_total=500, queues=[queue()])]
        result = rank_placements(clusters, PlacementRequest(cores=5000, hours=1))
        assert result["candidates"] == []
        assert "5,000 cores" in result["blocked"][0]["blockers"][0]

    def test_queue_core_cap_blocks(self):
        """A transfer queue on a big cluster must not win a 1k-core job."""
        clusters = [
            cluster(
                "big",
                cores_total=100000,
                queues=[
                    queue("transfer", max_cores=16),
                    queue("standard", max_cores=50000),
                ],
            )
        ]
        result = rank_placements(clusters, PlacementRequest(cores=1024, hours=2))
        assert [c["queue"] for c in result["candidates"]] == ["standard"]
        assert "caps jobs at 16 cores" in result["blocked"][0]["blockers"][0]

    def test_unknown_core_cap_does_not_block(self):
        clusters = [cluster("a", cores_total=10000, queues=[queue(max_cores="-")])]
        result = rank_placements(clusters, PlacementRequest(cores=1024, hours=2))
        assert len(result["candidates"]) == 1

    def test_ties_break_on_idle_capacity(self):
        clusters = [
            cluster("small", cores_total=1000, cores_running=0, queues=[queue()]),
            cluster("large", cores_total=9000, cores_running=0, queues=[queue()]),
        ]
        result = rank_placements(clusters, PlacementRequest(cores=10, hours=1))
        assert [c["cluster"] for c in result["candidates"]] == ["large", "small"]

    def test_offline_cluster_is_blocked(self):
        clusters = [cluster("down", status="off", queues=[queue()])]
        result = rank_placements(clusters, PlacementRequest())
        assert "cluster status is OFF" in result["blocked"][0]["blockers"]

    def test_allocation_shortfall_blocks(self):
        clusters = [
            cluster("thin", cores_total=10000, hours_remaining=100, queues=[queue()])
        ]
        # 1000 cores x 10h = 10,000 core-hours, but only 100 remain.
        result = rank_placements(clusters, PlacementRequest(cores=1000, hours=10))
        assert result["candidates"] == []
        assert "core-hours" in result["blocked"][0]["blockers"][0]

    def test_exhausted_allocation_blocks(self):
        clusters = [cluster("empty", hours_remaining=0, queues=[queue()])]
        result = rank_placements(clusters, PlacementRequest(cores=1, hours=1))
        assert "no allocation hours remaining" in result["blocked"][0]["blockers"]

    def test_gpu_request_needs_gpus(self):
        clusters = [
            cluster("cpu-only", gpus=0, queues=[queue()]),
            cluster("gpu-box", gpus=8, queues=[queue()]),
        ]
        result = rank_placements(clusters, PlacementRequest(cores=1, hours=1, gpus=4))
        assert [c["cluster"] for c in result["candidates"]] == ["gpu-box"]
        assert "needs GPUs" in result["blocked"][0]["blockers"][0]

    def test_measured_wait_beats_unknown_wait(self):
        clusters = [
            cluster("fast", cores_total=1000, cores_running=900, queues=[queue(pending=50)]),
            cluster("slow", cores_total=1000, cores_running=900, queues=[queue(pending=50)]),
        ]
        estimates = {
            ("fast", "standard"): {"wait_seconds": 600, "wait_display": "~10 minutes"},
            ("slow", "standard"): {"wait_seconds": 12 * 3600, "wait_display": "~12 hours"},
        }
        result = rank_placements(
            clusters, PlacementRequest(cores=50, hours=1), wait_estimates=estimates
        )
        ranked = [c["cluster"] for c in result["candidates"]]
        assert ranked[0] == "fast"
        assert ranked[-1] == "slow"
        assert "estimated start ~10 minutes" in result["candidates"][0]["reasons"]

    def test_reasons_are_always_present_for_candidates(self):
        clusters = [cluster("a", cores_total=1000, queues=[queue()])]
        result = rank_placements(clusters, PlacementRequest(cores=10, hours=1))
        candidate = result["candidates"][0]
        assert candidate["reasons"]
        assert set(candidate["components"]) == {
            "capacity",
            "wait",
            "backlog",
            "allocation",
        }
        assert candidate["links"]["queues"] == "queues.html?cluster=a"

    def test_limit_is_respected(self):
        clusters = [
            cluster(f"c{i}", queues=[queue(f"q{i}")]) for i in range(10)
        ]
        result = rank_placements(clusters, PlacementRequest(), limit=3)
        assert len(result["candidates"]) == 3
        assert result["considered"] == 10

    def test_empty_fleet_is_safe(self):
        result = rank_placements([], PlacementRequest())
        assert result["candidates"] == []
        assert result["considered"] == 0


class TestPlacementRequest:
    def test_from_query_params(self):
        request = PlacementRequest.from_params(
            {"cores": ["256"], "hours": ["3.5"], "gpus": ["4"]}
        )
        assert (request.cores, request.hours, request.gpus) == (256, 3.5, 4)
        assert request.core_hours == 896

    def test_bad_input_falls_back_to_defaults(self):
        request = PlacementRequest.from_params({"cores": ["abc"], "hours": [""]})
        assert request.cores == 1
        assert request.hours == 1.0

    def test_values_are_clamped(self):
        request = PlacementRequest.from_params({"cores": ["-5"], "hours": ["99999"]})
        assert request.cores == 1
        assert request.hours == 24 * 30
