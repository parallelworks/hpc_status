"""Tests for the shared fleet insight engine.

This logic used to live inside the /api/insights request handler; these
tests pin the behaviour now that the topology view consumes it too.
"""

import pytest

from src.insights.engine import generate_fleet_insights


def cluster(**overrides):
    base = {
        "cluster_metadata": {"name": "narwhal", "status": "on"},
        "usage_data": {"systems": []},
        "queue_data": {"queues": [], "nodes": []},
        "gpu_data": {"summary": {}},
    }
    base.update(overrides)
    return base


class TestClusterInsights:
    def test_offline_cluster_is_flagged(self):
        insights = generate_fleet_insights(
            None, [cluster(cluster_metadata={"name": "narwhal", "status": "off"})]
        )
        assert any(i["metric"] == "status" and i["priority"] == 4 for i in insights)

    def test_active_status_is_not_flagged(self):
        # PW reports ACTIVE; the old code treated anything but ON as an outage.
        for status in ("on", "ACTIVE", "up", "RUNNING", "online"):
            insights = generate_fleet_insights(
                None, [cluster(cluster_metadata={"name": "n", "status": status})]
            )
            assert not [i for i in insights if i["metric"] == "status"], status

    @pytest.mark.parametrize(
        "remaining,expected_priority",
        [(40, 5), (150, 3), (900, None)],
    )
    def test_allocation_thresholds(self, remaining, expected_priority):
        insights = generate_fleet_insights(
            None,
            [
                cluster(
                    usage_data={
                        "systems": [
                            {
                                "hours_allocated": "1,000",
                                "hours_remaining": str(remaining),
                            }
                        ]
                    }
                )
            ],
        )
        allocation = [i for i in insights if i["metric"] == "allocation"]
        if expected_priority is None:
            assert allocation == []
        else:
            assert allocation[0]["priority"] == expected_priority

    def test_backlog_is_relative_to_cluster_capacity(self):
        # 300 pending cores is a crisis on a 1k-core cluster...
        small = generate_fleet_insights(
            None,
            [
                cluster(
                    queue_data={
                        "queues": [
                            {"queue_name": "std", "cores_pending": 300, "cores_running": 700}
                        ],
                        "nodes": [{"cores_available": 1000}],
                    }
                )
            ],
        )
        assert [i for i in small if i["metric"] == "queue_depth"][0]["type"] == "warning"

        # ...and noise on a 100k-core one.
        large = generate_fleet_insights(
            None,
            [
                cluster(
                    queue_data={
                        "queues": [
                            {"queue_name": "std", "cores_pending": 300, "cores_running": 70000}
                        ],
                        "nodes": [{"cores_available": 100000}],
                    }
                )
            ],
        )
        assert not [i for i in large if i["metric"] == "queue_depth"]

    def test_gpu_utilization_extremes(self):
        busy = generate_fleet_insights(
            None,
            [cluster(gpu_data={"summary": {"gpu_count": 8, "avg_utilization_percent": 97}})],
        )
        idle = generate_fleet_insights(
            None,
            [cluster(gpu_data={"summary": {"gpu_count": 8, "avg_utilization_percent": 2}})],
        )
        assert "High GPU utilization" in busy[0]["message"]
        assert "mostly idle" in idle[0]["message"]

    def test_results_are_sorted_by_priority(self):
        insights = generate_fleet_insights(
            None,
            [
                cluster(
                    cluster_metadata={"name": "narwhal", "status": "off"},
                    usage_data={
                        "systems": [{"hours_allocated": "1000", "hours_remaining": "10"}]
                    },
                    gpu_data={"summary": {"gpu_count": 4, "avg_utilization_percent": 1}},
                )
            ],
        )
        priorities = [i["priority"] for i in insights]
        assert priorities == sorted(priorities, reverse=True)

    def test_empty_inputs_are_safe(self):
        assert generate_fleet_insights(None, None) == []
        assert generate_fleet_insights({}, []) == []
