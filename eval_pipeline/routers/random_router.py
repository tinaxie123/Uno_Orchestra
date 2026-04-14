"""
Random routing baseline — randomly pick model for each query.
Tests: "Does learned routing beat random?"
"""
import random
import openai
from .base import BaseRouter, RouteResult
from ..config import MODEL_POOL, COST_PER_M, EVAL_MAX_TOKENS, SUB_AGENT_TEMP, DEFAULT_API_BASE, resolve_model


class RandomRouter(BaseRouter):
    """Random model selection — no intelligence in routing."""

    def __init__(self, api_base=DEFAULT_API_BASE, api_key="EMPTY", seed=42):
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)
        self.rng = random.Random(seed)

    @property
    def name(self):
        return "Random"

    def route(self, question: str, context: dict = None) -> RouteResult:
        mid = self.rng.choice(MODEL_POOL)
        actual = resolve_model(mid)
        try:
            r = self.api.chat.completions.create(
                model=actual,
                messages=[{"role": "user", "content": question}],
                temperature=SUB_AGENT_TEMP, max_tokens=EVAL_MAX_TOKENS,
            )
            txt = r.choices[0].message.content or ""
            toks = getattr(r.usage, "completion_tokens", 0) or 0
            cost = COST_PER_M.get(mid, 10.0) * max(toks, 1) / 1e6
            return RouteResult(answer=txt, route_count=1, routed_models=[mid],
                               total_cost=cost, total_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=1, routed_models=[mid])
