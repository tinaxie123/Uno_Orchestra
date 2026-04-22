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

    # ----------------------------------------------------------------
    # Multi-turn interface (used by interactive benchmarks like
    # Terminal-Bench). Routers that wrap a single chat-completions-style
    # API (Direct, Random, Oracle) inherit the default; composite routers
    # (Planner, SkillRouterSFT) can override with their own orchestration.
    # ----------------------------------------------------------------

    def chat_completions(self, messages, tools=None, **kwargs):
        """Multi-turn chat-completions call with optional tool definitions.

        Returns a dict-shaped response:
            {
                "content": str | None,
                "tool_calls": [
                    {"id": str, "name": str, "arguments": dict},
                    ...
                ],
                "completion_tokens": int,
                "model": str,
            }

        Default implementation raises ``NotImplementedError``; routers that
        participate in interactive benchmarks override this method.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement chat_completions(); "
            "override this method to use interactive benchmarks."
        )
