"""
Random routing baseline — randomly pick model for each query.
Tests: "Does learned routing beat random?"
"""
import random
import openai
from .base import BaseRouter, RouteResult
from ..config import MODEL_POOL, EVAL_MAX_TOKENS, SUB_AGENT_TEMP, DEFAULT_API_BASE, resolve_model, compute_cost


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
            prompt_toks = getattr(r.usage, "prompt_tokens", 0) or 0
            cost = compute_cost(mid, toks, prompt_toks)
            return RouteResult(answer=txt, route_count=1, routed_models=[mid],
                               total_cost=cost, total_tokens=toks + prompt_toks,
                               prompt_tokens=prompt_toks, completion_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=1, routed_models=[mid])

    def chat_completions(self, messages, tools=None, **kw):
        """Random model per turn. Useful for baselines where the planner picks a
        worker uniformly at random each delegation."""
        import json as _json
        mid = self.rng.choice(MODEL_POOL)
        actual = resolve_model(mid)
        call_kw = dict(
            model=actual,
            messages=messages,
            temperature=kw.get("temperature", SUB_AGENT_TEMP),
            max_tokens=kw.get("max_tokens", EVAL_MAX_TOKENS),
        )
        if tools:
            call_kw["tools"] = tools
            call_kw["tool_choice"] = kw.get("tool_choice", "auto")
        r = self.api.chat.completions.create(**call_kw)
        msg = r.choices[0].message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = _json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {"_raw": tc.function.arguments}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {
            "content": msg.content,
            "tool_calls": tool_calls,
            "completion_tokens": getattr(r.usage, "completion_tokens", 0) or 0,
            "prompt_tokens": getattr(r.usage, "prompt_tokens", 0) or 0,
            "model": mid,
        }
