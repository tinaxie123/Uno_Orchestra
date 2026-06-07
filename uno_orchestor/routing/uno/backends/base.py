"""Backend interface for UNO routing primitives."""

from __future__ import annotations

from typing import Protocol

from uno_orchestor.routing.uno.primitives import PrimitiveResult, Route


class PrimitiveBackend(Protocol):
    name: str

    def run(self, route: Route, question: str) -> PrimitiveResult | None:
        """Return a result when this backend handles the route, otherwise None."""
