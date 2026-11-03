"""Token Mentor: auditable model pricing and project-aware recommendations."""

from .models import BenchmarkEvidence, ModelQuote, ProjectAnalysis
from token_mentor.orchestration.comparison import ComparisonRequest, ComparisonWorkflow
from token_mentor.orchestration.agents import EvaluationAgent, MarketResearchAgent, ProjectAdvisorAgent
from token_mentor.orchestration.service import RecommendationService

__all__ = [
    "BenchmarkEvidence", "ComparisonRequest", "ComparisonWorkflow", "EvaluationAgent", "MarketResearchAgent", "ModelQuote", "ProjectAdvisorAgent",
    "ProjectAnalysis", "RecommendationService",
]
__version__ = "0.1.0"

