from .base import BaseRouter, RouteResult
from .local_router import LocalRouter
from .direct import DirectRouter
from .random_router import RandomRouter
from .oracle import OracleRouter, cheapest_router, router_plus_claude, codex_router
from .router_sft import UnoSFT
from .planner_router import PlannerRouter

ROUTER_REGISTRY = {
    # Full Planner → Router → Worker pipeline (real framework)
    "planner": PlannerRouter,
    # Simplified routers (for baselines)
    "local": LocalRouter,
    "direct": DirectRouter,
    "random": RandomRouter,
    "uno-sft": UnoSFT,
    "oracle-cheapest": cheapest_router,
    "router+claude": router_plus_claude,
    "oracle-codex": codex_router,
}
