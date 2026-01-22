"""Tests for the recommendations engine."""

import pytest
from src.insights.recommendations import (
    RecommendationEngine,
    JobRequirements,
)


class TestRecommendationEngine:
    @pytest.fixture
    def sample_systems(self):
        return [
            {
                "cluster": "nautilus",
                "status": "on",
                "queues": [
                    {
                        "name": "standard",
                        "max_walltime": "24:00:00",
                        "queue_type": "batch",
                        "jobs": {"running": 10, "pending": 0},
                        "cores": {"running": 960, "pending": 0},
                        "utilization_percent": 30,
                    },
                    {
                        "name": "debug",
                        "max_walltime": "01:00:00",
                        "queue_type": "debug",
                        "jobs": {"running": 0, "pending": 0},
                        "cores": {"running": 0, "pending": 0},
                        "utilization_percent": 0,
                    },
                ],
                "usage": {
                    "percent_remaining": 85,
                    "total_allocated_hours": 250000,
                    "total_remaining_hours": 212500,
                },
            },
            {
                "cluster": "jean",
                "status": "on",
                "queues": [
                    {
                        "name": "compute",
                        "max_walltime": "48:00:00",
                        "queue_type": "batch",
                        "jobs": {"running": 20, "pending": 15},
                        "cores": {"running": 1920, "pending": 1440},
                        "utilization_percent": 75,
                    },
                ],
                "usage": {
                    "percent_remaining": 45,
                    "total_allocated_hours": 100000,
                    "total_remaining_hours": 45000,
                },
            },
            {
                "cluster": "offline",
                "status": "off",
                "queues": [],
            },
        ]

    def test_recommend_queue_basic(self, sample_systems):
        engine = RecommendationEngine(sample_systems)
        requirements = JobRequirements(cores=32, walltime_hours=4)

        recommendations = engine.recommend_queue(requirements)

        assert len(recommendations) > 0
        # First recommendation should be nautilus (better score)
        assert recommendations[0].system in ("nautilus", "jean")

    def test_recommend_queue_respects_walltime(self, sample_systems):
        engine = RecommendationEngine(sample_systems)

        # Short job should not exclude debug queue
        short_job = JobRequirements(cores=8, walltime_hours=0.5)
        recs = engine.recommend_queue(short_job)
        queues = [r.queue for r in recs]
        assert "debug" in queues or "standard" in queues

        # Long job should exclude debug queue
        long_job = JobRequirements(cores=32, walltime_hours=36)
        recs = engine.recommend_queue(long_job)
        queues = [r.queue for r in recs]
        assert "debug" not in queues

    def test_recommend_queue_excludes_offline_systems(self, sample_systems):
        engine = RecommendationEngine(sample_systems)
        requirements = JobRequirements(cores=32, walltime_hours=4)

        recommendations = engine.recommend_queue(requirements)
        systems = [r.system for r in recommendations]

        assert "offline" not in systems

    def test_recommend_queue_includes_wait_estimate(self, sample_systems):
        engine = RecommendationEngine(sample_systems)
        requirements = JobRequirements(cores=32, walltime_hours=4)

        recommendations = engine.recommend_queue(requirements)

        assert all(r.estimated_wait_minutes is not None for r in recommendations)

    def test_suggest_load_balance(self, sample_systems):
        engine = RecommendationEngine(sample_systems)
        requirements = JobRequirements(cores=32, walltime_hours=4)

        result = engine.suggest_load_balance(100, requirements)

        assert "distribution" in result
        assert len(result["distribution"]) > 0
        assert "confidence" in result

        # Total jobs should equal requested amount
        total = sum(d["jobs"] for d in result["distribution"].values())
        assert total == 100

    def test_generate_insights_allocation_warning(self, sample_systems):
        # Modify sample to have low allocation
        sample_systems[0]["usage"]["percent_remaining"] = 8
        engine = RecommendationEngine(sample_systems)

        insights = engine.generate_insights()

        allocation_insights = [i for i in insights if i.related_metric == "allocation"]
        assert len(allocation_insights) > 0
        assert any("critically low" in i.message.lower() for i in allocation_insights)

    def test_generate_insights_queue_depth(self, sample_systems):
        # Add high queue depth
        sample_systems[1]["queues"][0]["jobs"]["pending"] = 100
        engine = RecommendationEngine(sample_systems)

        insights = engine.generate_insights()

        queue_insights = [i for i in insights if i.related_metric == "queue_depth"]
        assert len(queue_insights) > 0

    def test_parse_walltime(self, sample_systems):
        engine = RecommendationEngine(sample_systems)

        assert engine._parse_walltime("24:00:00") == 24.0
        assert engine._parse_walltime("01:30:00") == 1.5
        assert engine._parse_walltime("-") is None
        assert engine._parse_walltime("") is None
        assert engine._parse_walltime("invalid") is None


class TestJobRequirements:
    def test_defaults(self):
        req = JobRequirements(cores=32, walltime_hours=4)
        assert req.cores == 32
        assert req.walltime_hours == 4
        assert req.memory_gb is None
        assert req.gpus == 0
        assert req.priority == "normal"

    def test_with_gpus(self):
        req = JobRequirements(cores=32, walltime_hours=4, gpus=2)
        assert req.gpus == 2

    def test_with_memory(self):
        req = JobRequirements(cores=32, walltime_hours=4, memory_gb=128)
        assert req.memory_gb == 128
