"""Composable skill interface for UNO route implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from uno_orchestor.routing.uno.primitives import PrimitiveResult, Route


RunPrimitive = Callable[[str, str], PrimitiveResult]


@dataclass(frozen=True)
class SkillContext:
    question: str
    run_primitive: RunPrimitive


class SkillImplementation(Protocol):
    id: str

    def run(self, route: Route, ctx: SkillContext) -> PrimitiveResult:
        """Run a public route skill by composing one or more primitives."""
