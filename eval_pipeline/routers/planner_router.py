"""Compatibility entrypoint for the paper-style Uno evaluation router.

Historically the CLI name ``--router planner`` pointed at an older
Planner -> Router -> Worker implementation.  The paper
architecture is a unified policy that emits decomposition and routing decisions
in one assistant stream, so this compatibility class now reuses the same Uno
schema/harness path as ``UnoSFT`` while preserving the old CLI surface.
"""

from __future__ import annotations

import json
import openai

from .router_sft import UnoSFT
from ..config import DEFAULT_API_BASE, DEFAULT_LOCAL_BASE, EVAL_MAX_TOKENS


class PlannerRouter(UnoSFT):
    """Paper-style unified Uno router, kept under the old ``planner`` name."""

    def __init__(
        self,
        planner_model: str = "Qwen/Qwen2.5-7B-Instruct",
        router_model: str | None = None,
        planner_api_base: str = DEFAULT_LOCAL_BASE,
        router_api_base: str | None = None,
        sub_model_api_base: str = DEFAULT_API_BASE,
        planner_api_key: str = "EMPTY",
        router_api_key: str = "EMPTY",
        sub_model_api_key: str = "EMPTY",
        planner_temperature: float = 0.0,
        router_temperature: float = 0.0,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        model_name = router_model or planner_model
        local_base = router_api_base or planner_api_base
        super().__init__(
            local_base=local_base,
            api_base=sub_model_api_base or api_base or DEFAULT_API_BASE,
            api_key=sub_model_api_key or api_key or "EMPTY",
            model_name=model_name,
        )
        self.planner_model = planner_model
        self.router_model = model_name
        self.planner_temperature = planner_temperature
        self.router_temperature = router_temperature
        self.chat_client = openai.OpenAI(base_url=planner_api_base, api_key=planner_api_key)

    @property
    def name(self) -> str:
        p = self.planner_model.split("/")[-1]
        r = self.router_model.split("/")[-1]
        return f"UnoRouter({p})" if p == r else f"UnoRouter(P={p},R={r})"

    def chat_completions(self, messages, tools=None, **kw):
        """Expose raw chat completions for interactive Docker benchmarks."""
        call_kw = dict(
            model=self.planner_model,
            messages=messages,
            temperature=kw.get("temperature", self.planner_temperature),
            max_tokens=kw.get("max_tokens", EVAL_MAX_TOKENS),
        )
        if tools:
            call_kw["tools"] = tools
            call_kw["tool_choice"] = kw.get("tool_choice", "auto")
        r = self.chat_client.chat.completions.create(**call_kw)
        msg = r.choices[0].message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {"_raw": tc.function.arguments}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {
            "content": msg.content,
            "tool_calls": tool_calls,
            "completion_tokens": getattr(r.usage, "completion_tokens", 0) or 0,
            "prompt_tokens": getattr(r.usage, "prompt_tokens", 0) or 0,
            "model": self.planner_model,
        }
