from .base import BaseRouter, RouteResult
from .local_router import LocalRouter
from .direct import DirectRouter
from .random_router import RandomRouter
from .oracle import OracleRouter, cheapest_router, strongest_router, codex_router

ROUTER_REGISTRY = {
    "local": LocalRouter,
    "direct": DirectRouter,
    "random": RandomRouter,
    "oracle-cheapest": cheapest_router,
    "oracle-strongest": strongest_router,
    "oracle-codex": codex_router,
}
