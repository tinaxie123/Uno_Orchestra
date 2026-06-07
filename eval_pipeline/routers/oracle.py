import openai
from .base import BaseRouter, RouteResult
from ..config import EVAL_MAX_TOKENS, DEFAULT_API_BASE, resolve_model, compute_cost


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
            prompt_toks = getattr(r.usage, "prompt_tokens", 0) or 0
            cost = compute_cost(self.model_id, toks, prompt_toks)
            return RouteResult(answer=txt, route_count=1, routed_models=[self.model_id],
                               total_cost=cost, total_tokens=toks + prompt_toks,
                               prompt_tokens=prompt_toks, completion_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=1, routed_models=[self.model_id])

    def chat_completions(self, messages, tools=None, **kw):
        import json as _json
        actual = resolve_model(self.model_id)
        call_kw = dict(
            model=actual,
            messages=messages,
            temperature=kw.get("temperature", 0.0),
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
            "model": self.model_id,
        }


def cheapest_router(**kw):
    return OracleRouter("gemini-2.5-flash-lite", "Cheapest(gemini-flash-lite)", **kw)

def router_plus_claude(**kw):
    return OracleRouter("claude-opus-4-6", "router+claude", **kw)

def codex_router(**kw):
    return OracleRouter("gpt-5.3-codex", "Codex-Only", **kw)

# Backward-compat alias: the previous name used in notebooks / scripts.
strongest_router = router_plus_claude
SingleRouter = OracleRouter
