"""Local YouTube intelligence services shared by UI, agents, and APIs."""

from beacon_engine.intelligence.workspaces import NicheWorkspaceService
from beacon_engine.intelligence.metrics import ChannelMetricsService
from beacon_engine.intelligence.patterns import PatternIntelligenceService
from beacon_engine.intelligence.recommendations import IntelligenceRecommendationService

__all__ = [
    "ChannelMetricsService",
    "IntelligenceRecommendationService",
    "NicheWorkspaceService",
    "PatternIntelligenceService",
]
