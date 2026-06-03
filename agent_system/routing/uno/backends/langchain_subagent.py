"""LangChain-backed sub-agent backend for UNO primitives."""

from __future__ import annotations

import os
from typing import Any, Callable

from agent_system.routing.uno.primitives import (
    PRIMITIVE_PROMPTS,
    PRIMITIVES,
    PrimitiveResult,
    Route,
)


class LangChainSubAgentBackend:
    name = "langchain_subagent"

    def __init__(
        self,
        model_max_tokens: dict[str, int] | None = None,
        default_max_tokens: int = 256,
        temperature: float = 0.3,
        api_key: str | None = None,
        base_url: str | None = None,
        model_resolver: Callable[[str], str] | None = None,
    ):
        self.model_max_tokens = model_max_tokens or {}
        self.default_max_tokens = default_max_tokens
        self.temperature = temperature
        self.api_key = api_key
        self.base_url = base_url
        self.model_resolver = model_resolver or (lambda model: model)
        self._clients: dict[tuple[str, int], Any] = {}

    def run(self, route: Route, question: str) -> PrimitiveResult | None:
        if route.skill not in PRIMITIVES:
            return None

        client = self._client(route.model)
        sys_prompt = PRIMITIVE_PROMPTS.get(route.skill, "Answer the following question concisely.")
        user_content = (
            f"Original question: {question}\n\nSub-task: {route.query}\n\nAnswer directly, no chain of thought."
            if question
            else f"Sub-task: {route.query}\n\nAnswer directly, no chain of thought."
        )

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError as exc:
            raise RuntimeError(
                "LangChain sub-agent backend requires langchain-core. "
                "Install project dependencies from pyproject.toml."
            ) from exc

        response = client.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_content)])
        text = _content_to_text(getattr(response, "content", ""))
        output_tokens = _output_tokens(response)
        return PrimitiveResult(
            text=text.strip(),
            output_tokens=output_tokens,
            billable=True,
            backend=self.name,
        )

    def _client(self, model: str):
        max_tokens = self.model_max_tokens.get(model, self.default_max_tokens)
        actual_model = self.model_resolver(model)
        key = (actual_model, max_tokens)
        if key in self._clients:
            return self._clients[key]

        api_key = self.api_key or os.environ.get("REMOTE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "REMOTE_API_KEY is not set. Export it before running the LangChain sub-agent backend."
            )
        base_url = self.base_url or os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")

        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "LangChain sub-agent backend requires langchain-openai. "
                "Install project dependencies from pyproject.toml."
            ) from exc

        client = ChatOpenAI(
            model=actual_model,
            api_key=api_key,
            base_url=base_url,
            temperature=self.temperature,
            max_tokens=max_tokens,
            timeout=60.0,
            max_retries=1,
        )
        self._clients[key] = client
        return client


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _output_tokens(response: Any) -> int:
    usage_metadata = getattr(response, "usage_metadata", None) or {}
    for key in ("output_tokens", "completion_tokens"):
        value = usage_metadata.get(key)
        if value is not None:
            return int(value)

    response_metadata = getattr(response, "response_metadata", None) or {}
    token_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    for key in ("completion_tokens", "output_tokens"):
        value = token_usage.get(key)
        if value is not None:
            return int(value)
    return 0
