import openai
from .base import BaseRouter, RouteResult
from ..config import COST_PER_M, EVAL_MAX_TOKENS, DEFAULT_API_BASE, resolve_model


class OracleRouter(BaseRouter):
    
    def __init__(self, model_id: str, label: str = None,
                 api_base=DEFAULT_API_BASE, api_key="EMPTY"):
        self.model_id = model_id
        self._label = label or f"Oracle({model_id})"
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)

    @property
    def name(self):
        return self._label

    def route(self, question: str, context: dict = None) -> RouteResult:
        actual = resolve_model(self.model_id)
        try:
            r = self.api.chat.completions.create(
                model=actual,
                messages=[{"role": "user", "content": question}],
                temperature=0.0, max_tokens=EVAL_MAX_TOKENS,
            )
            txt = r.choices[0].message.content or ""
            toks = getattr(r.usage, "completion_tokens", 0) or 0
            cost = COST_PER_M.get(self.model_id, 10.0) * max(toks, 1) / 1e6
            return RouteResult(answer=txt, route_count=1, routed_models=[self.model_id],
                               total_cost=cost, total_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=1, routed_models=[self.model_id])


def cheapest_router(**kw):
    return OracleRouter("claude-haiku-4-5-20251001", "Cheapest(haiku)", **kw)

def strongest_router(**kw):
    return OracleRouter("claude-opus-4-6", "Strongest(opus)", **kw)

def codex_router(**kw):
    return OracleRouter("gpt-5.3-codex", "Codex-Only", **kw)
SingleRouter = OracleRouter
