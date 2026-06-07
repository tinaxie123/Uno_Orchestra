"""
Abstract router interface. All routers (Uno variants, baselines,
prior-art routers run for comparison) implement this.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List


@dataclass
class RouteResult:

    answer: str
    full_trace: str = ""
    route_count: int = 0
    routed_models: List[str] = field(default_factory=list)
    routed_skills: List[str] = field(default_factory=list)
    routed_backends: List[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class BaseRouter(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for reports."""
        ...

    @abstractmethod
    def route(self, question: str, context: dict = None) -> RouteResult:

        ...
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
