"""Insights module - recommendations, load balancing, and analytics."""

from .recommendations import RecommendationEngine, JobRequirements, QueueRecommendation

__all__ = [
    "RecommendationEngine",
    "JobRequirements",
    "QueueRecommendation",
]
