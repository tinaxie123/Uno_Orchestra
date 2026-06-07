"""MCP server exposing UNO routing primitives.

Run with:
    python -m uno_orchestor.routing.uno.mcp_server

The server is intentionally thin: every tool delegates to RouteHarness, so MCP,
RL training, and eval share the same validation and backend dispatch path.
"""

from __future__ import annotations

import argparse
from typing import Any

from uno_orchestor.routing.uno.harness import (
    RouteValidationError,
    build_default_harness,
)
from uno_orchestor.routing.uno.primitives import PRIMITIVES, Route


def _mcp_import():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "UNO MCP server requires the official 'mcp' package. "
            "Install with `pip install mcp` or the project's MCP extra."
        ) from exc
    return FastMCP


def create_server(name: str = "uno-routing-primitives"):
    FastMCP = _mcp_import()
    server = FastMCP(name)
    harness = build_default_harness()

    @server.tool()
    def route(
        model: str,
        skill: str,
        query: str,
        question: str = "",
        round: int = 1,
        subtask: int = 1,
    ) -> dict[str, Any]:
        """Run one UNO primitive route and return a normalized observation."""
        try:
            result = harness.run_route(
                Route(round=round, subtask=subtask, model=model, skill=skill, query=query),
                question,
            )
        except RouteValidationError as exc:
            return {
                "ok": False,
                "error": "invalid_route",
                "detail": str(exc),
                "backend": "harness",
            }
        return {
            "ok": not result.text.startswith("<error "),
            "text": result.text,
            "backend": result.backend,
            "output_tokens": result.output_tokens,
            "billable": result.billable,
            "timed_out": result.timed_out,
        }

    @server.tool()
    def list_primitives() -> list[dict[str, str]]:
        """List the closed UNO primitive vocabulary and contracts."""
        return [
            {
                "id": spec.id,
                "cluster": spec.cluster,
                "contract": spec.contract,
                "backend": spec.backend,
            }
            for spec in PRIMITIVES.values()
        ]

    for skill_name in PRIMITIVES:
        _register_skill_tool(server, harness, skill_name)

    return server


def _register_skill_tool(server, harness, skill_name: str) -> None:
    def run_skill(
        query: str,
        model: str,
        question: str = "",
        round: int = 1,
        subtask: int = 1,
    ) -> dict[str, Any]:
        try:
            result = harness.run_route(
                Route(round=round, subtask=subtask, model=model, skill=skill_name, query=query),
                question,
            )
        except RouteValidationError as exc:
            return {
                "ok": False,
                "error": "invalid_route",
                "detail": str(exc),
                "backend": "harness",
            }
        return {
            "ok": not result.text.startswith("<error "),
            "text": result.text,
            "backend": result.backend,
            "output_tokens": result.output_tokens,
            "billable": result.billable,
            "timed_out": result.timed_out,
        }

    run_skill.__name__ = skill_name
    run_skill.__doc__ = PRIMITIVES[skill_name].contract
    server.tool()(run_skill)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the UNO MCP primitive server.")
    parser.add_argument("--name", default="uno-routing-primitives")
    args = parser.parse_args()
    create_server(args.name).run()


if __name__ == "__main__":
    main()
