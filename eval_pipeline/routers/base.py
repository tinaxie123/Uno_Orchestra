"""
Abstract router interface. All routers (Router-R1, SkillRouter, baselines) implement this.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List


@dataclass
class RouteResult:
    """Standardized output from any router."""
    answer: str                          # Final answer text
    full_trace: str = ""                 # Full routing trace (for debugging)
    route_count: int = 0                 # Number of sub-agent calls
    routed_models: List[str] = field(default_factory=list)  # Which models were called
    routed_skills: List[str] = field(default_factory=list)  # Which skills were used
    total_cost: float = 0.0              # Total API cost (USD)
    total_tokens: int = 0                # Total output tokens from sub-agents


class BaseRouter(ABC):
    """All routers implement: name, route()."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for reports."""
        ...

    @abstractmethod
    def route(self, question: str, context: dict = None) -> RouteResult:
        """
        Route a question. Returns standardized RouteResult.

        Args:
            question: The task/question to solve
            context: Optional benchmark-specific context (e.g. repo name, task instruction)
        """
        ...
