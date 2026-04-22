from .base import BaseRouter, RouteResult
from .local_router import LocalRouter
from .direct import DirectRouter
from .random_router import RandomRouter
from .oracle import OracleRouter, cheapest_router, strongest_router, codex_router
from .router_sft import SkillRouterSFT
from .planner_router import PlannerRouter

ROUTER_REGISTRY = {
    # Full Planner → Router → Worker pipeline (real framework)
    "planner": PlannerRouter,
    # Simplified routers (for baselines)
    "local": LocalRouter,
    "direct": DirectRouter,
    "random": RandomRouter,
    "skill-sft": SkillRouterSFT,
    "oracle-cheapest": cheapest_router,
    "oracle-strongest": strongest_router,
    "oracle-codex": codex_router,
}
