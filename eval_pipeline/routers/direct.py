"""
Direct prompting baseline — single model, no routing.
Tests: "Is routing worth it at all?"
"""
import openai
from .base import BaseRouter, RouteResult
from ..config import COST_PER_M, EVAL_MAX_TOKENS, DEFAULT_API_BASE


class DirectRouter(BaseRouter):
    """No routing: send question directly to a single model."""

    def __init__(self, model_id: str, api_base=DEFAULT_API_BASE, api_key="EMPTY",
                 system_prompt="You are a helpful assistant."):
        self.model_id = model_id
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)
        self.system_prompt = system_prompt

    @property
    def name(self):
        return f"Direct({self.model_id})"

    def route(self, question: str, context: dict = None) -> RouteResult:
        try:
            r = self.api.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": question},
                ],
                temperature=0.0, max_tokens=EVAL_MAX_TOKENS,
            )
            txt = r.choices[0].message.content or ""
            toks = getattr(r.usage, "completion_tokens", 0) or 0
            cost = COST_PER_M.get(self.model_id, 10.0) * max(toks, 1) / 1e6
            return RouteResult(answer=txt, route_count=0, routed_models=[self.model_id],
                               total_cost=cost, total_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=0)
