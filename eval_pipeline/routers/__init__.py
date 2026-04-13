from .base import BaseRouter, RouteResult
from .router_r1 import RouterR1
from .direct import DirectRouter
from .random_router import RandomRouter
from .oracle import OracleRouter, cheapest_router, strongest_router, codex_router
from .skillrouter_sft import SkillRouterSFT

ROUTER_REGISTRY = {
    "router-r1": RouterR1,
    "skillrouter-sft": SkillRouterSFT,
    "direct": DirectRouter,
    "random": RandomRouter,
    "oracle-cheapest": cheapest_router,
    "oracle-strongest": strongest_router,
    "oracle-codex": codex_router,
}
