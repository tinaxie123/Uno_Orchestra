"""Router: model + skill selection for each subtask via LangChain.

Provides both sync (route_subtask) and async (aroute_subtask) entry points.
"""

from __future__ import annotations

import logging
from difflib import get_close_matches

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI

from configs import PoolConfig
from scripts.data.prompts import build_system_prompt

logger = logging.getLogger(__name__)


def _make_route_tool(pools: PoolConfig):
    @lc_tool
    def route(model: str, skill: str) -> str:
        """Select the model and skill for this sub-task.

        Args:
            model: One of the available models.
            skill: One of the available skills.
        """
        return f"routed to {model} / {skill}"
    return route


def _validate_routing(
    selected_model: str, selected_skill: str, pools: PoolConfig,
) -> tuple[str, str]:
    if selected_model not in set(pools["models"]):
        matches = get_close_matches(selected_model, pools["models"], n=1, cutoff=0.5)
        selected_model = matches[0] if matches else pools["models"][0]
    if selected_skill not in set(pools["skills"]):
        matches = get_close_matches(selected_skill, pools["skills"], n=1, cutoff=0.5)
        selected_skill = matches[0] if matches else "direct_answer"
    allowed = pools["model_skills"].get(selected_model, [])
    if allowed and selected_skill not in allowed:
        selected_skill = allowed[0] if allowed else "direct_answer"
    return selected_model, selected_skill


def _is_local(api_base: str) -> bool:
    return "localhost" in api_base or "127.0.0.1" in api_base


def _make_llm(model, api_base, api_key, temperature, pools):
    extra = {"max_tokens": 256}
    if not _is_local(api_base):
        extra["enable_thinking"] = False
    return ChatOpenAI(
        model=model,
        base_url=api_base,
        api_key=api_key or "none",
        temperature=temperature,
        timeout=60,
        model_kwargs={"extra_body": extra},
    ).bind_tools([_make_route_tool(pools)])


def _extract(ai_msg, pools):
    if ai_msg.tool_calls:
        args = ai_msg.tool_calls[0]["args"]
        return _validate_routing(args.get("model", ""), args.get("skill", ""), pools)
    return pools["models"][0], "direct_answer"


def _messages(pools, instruction):
    return [
        SystemMessage(content=build_system_prompt("router", pools=pools)),
        HumanMessage(content=instruction),
    ]


# --- Sync ---

def route_subtask(
    instruction: str, model: str, api_base: str, api_key: str,
    pools: PoolConfig, temperature: float = 0.3,
) -> tuple[str, str]:
    llm = _make_llm(model, api_base, api_key, temperature, pools)
    try:
        ai_msg = llm.invoke(_messages(pools, instruction))
        return _extract(ai_msg, pools)
    except Exception as e:
        logger.warning("[Router] Error: %s", e)
    return pools["models"][0], "direct_answer"


# --- Async ---

async def aroute_subtask(
    instruction: str, model: str, api_base: str, api_key: str,
    pools: PoolConfig, temperature: float = 0.3,
) -> tuple[str, str]:
    llm = _make_llm(model, api_base, api_key, temperature, pools)
    try:
        ai_msg = await llm.ainvoke(_messages(pools, instruction))
        return _extract(ai_msg, pools)
    except Exception as e:
        logger.warning("[Router] Error: %s", e)
    return pools["models"][0], "direct_answer"
