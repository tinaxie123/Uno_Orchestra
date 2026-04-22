"""
PlannerRouter: wraps the full Planner → Router → Worker pipeline
from scripts/data/agent.py.

This is the REAL evaluation router that matches the training framework:
  Planner (plan_subtask/finish via LangChain tool calling)
    → Router (route(model, skill) via LangChain tool calling)
      → Worker API (9-model pool, real API calls)

All benchmark adapters in eval_pipeline/benchmarks/ can use this router
via the standard route() interface.
"""
import json
import asyncio
import logging
from .base import BaseRouter, RouteResult
from ..config import DEFAULT_LOCAL_BASE, DEFAULT_API_BASE

logger = logging.getLogger(__name__)


class PlannerRouter(BaseRouter):
    """Full Planner → Router → Worker pipeline as an eval router.

    Wraps scripts/data/agent.run_agent() to satisfy the BaseRouter interface.
    """

    def __init__(
        self,
        planner_model: str = "Qwen/Qwen2.5-7B-Instruct",
        router_model: str = "Qwen/Qwen2.5-7B-Instruct",
        planner_api_base: str = DEFAULT_LOCAL_BASE,
        router_api_base: str = DEFAULT_LOCAL_BASE,
        sub_model_api_base: str = DEFAULT_API_BASE,
        planner_api_key: str = "EMPTY",
        router_api_key: str = "EMPTY",
        sub_model_api_key: str = "EMPTY",
        planner_temperature: float = 0.7,
        router_temperature: float = 0.3,
        api_base: str = None,  # compat with build_router
        api_key: str = None,   # compat with build_router
    ):
        self.planner_model = planner_model
        self.router_model = router_model
        self.planner_api_base = planner_api_base
        self.router_api_base = router_api_base
        self.sub_model_api_base = sub_model_api_base or api_base or DEFAULT_API_BASE
        self.planner_api_key = planner_api_key
        self.router_api_key = router_api_key
        self.sub_model_api_key = sub_model_api_key or api_key or "EMPTY"
        self.planner_temperature = planner_temperature
        self.router_temperature = router_temperature

        # Load pool config
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from configs import load_pools
        self.pools = load_pools()

    @property
    def name(self) -> str:
        p = self.planner_model.split("/")[-1]
        r = self.router_model.split("/")[-1]
        if p == r:
            return f"PlannerRouter({p})"
        return f"PlannerRouter(P={p},R={r})"

    def route(self, question: str, context: dict = None) -> RouteResult:
        """Run the full Planner→Router→Worker pipeline synchronously."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from scripts.data.agent import run_agent

        try:
            result = run_agent(
                question=question,
                planner_model=self.planner_model,
                planner_api_base=self.planner_api_base,
                planner_api_key=self.planner_api_key,
                router_model=self.router_model,
                router_api_base=self.router_api_base,
                router_api_key=self.router_api_key,
                sub_model_api_base=self.sub_model_api_base,
                sub_model_api_key=self.sub_model_api_key,
                pools=self.pools,
                planner_temperature=self.planner_temperature,
                router_temperature=self.router_temperature,
            )
        except Exception as e:
            logger.error("[PlannerRouter] Pipeline error: %s", e)
            return RouteResult(answer=f"Error: {e}", full_trace=str(e))

        answer = result.get("answer", "")
        models = result.get("models_used", [])
        skills = result.get("skills_used", [])
        decisions = result.get("routing_decisions", [])
        messages = result.get("messages", [])

        # Build full trace from messages
        trace = json.dumps(messages, ensure_ascii=False, indent=1)[:5000]

        return RouteResult(
            answer=answer or "",
            full_trace=trace,
            route_count=len(models),
            routed_models=models,
            routed_skills=skills,
            total_cost=0.0,  # TODO: compute from pools pricing
            total_tokens=0,
        )

    async def aroute(self, question: str, context: dict = None) -> RouteResult:
        """Run the full pipeline asynchronously."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from scripts.data.agent import arun_agent

        try:
            result = await arun_agent(
                question=question,
                planner_model=self.planner_model,
                planner_api_base=self.planner_api_base,
                planner_api_key=self.planner_api_key,
                router_model=self.router_model,
                router_api_base=self.router_api_base,
                router_api_key=self.router_api_key,
                sub_model_api_base=self.sub_model_api_base,
                sub_model_api_key=self.sub_model_api_key,
                pools=self.pools,
                planner_temperature=self.planner_temperature,
                router_temperature=self.router_temperature,
            )
        except Exception as e:
            logger.error("[PlannerRouter] Async pipeline error: %s", e)
            return RouteResult(answer=f"Error: {e}", full_trace=str(e))

        answer = result.get("answer", "")
        models = result.get("models_used", [])
        skills = result.get("skills_used", [])
        messages = result.get("messages", [])
        trace = json.dumps(messages, ensure_ascii=False, indent=1)[:5000]

        return RouteResult(
            answer=answer or "",
            full_trace=trace,
            route_count=len(models),
            routed_models=models,
            routed_skills=skills,
            total_cost=0.0,
            total_tokens=0,
        )
