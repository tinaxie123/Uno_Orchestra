"""
Direct prompting baseline — single model, no routing.
Tests: "Is routing worth it at all?"
"""
import openai
from .base import BaseRouter, RouteResult
from ..config import EVAL_MAX_TOKENS, DEFAULT_API_BASE, compute_cost


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
            prompt_toks = getattr(r.usage, "prompt_tokens", 0) or 0
            cost = compute_cost(self.model_id, toks, prompt_toks)
            return RouteResult(answer=txt, route_count=0, routed_models=[self.model_id],
                               total_cost=cost, total_tokens=toks + prompt_toks,
                               prompt_tokens=prompt_toks, completion_tokens=toks)
        except Exception as e:
            return RouteResult(answer=f"Error: {e}", route_count=0)

    def chat_completions(self, messages, tools=None, **kw):
        """Pass-through to the underlying model, with optional tool definitions."""
        call_kw = dict(
            model=self.model_id,
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
        import json as _json
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
