"""Agent: Planner-Router-Executor pipeline.

Provides both sync and async entry points, plus a batch runner
that processes multiple questions concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from openai import AsyncOpenAI, OpenAI

from configs import PoolConfig
from scripts.data.planner import arun_planner, run_planner
from scripts.data.router import aroute_subtask, route_subtask

logger = logging.getLogger(__name__)

# Default concurrency for batch runs — tune to vLLM's max_num_seqs
DEFAULT_CONCURRENCY = 16


def _is_local(api_base: str) -> bool:
    return "localhost" in api_base or "127.0.0.1" in api_base


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def run_agent(
    question: str,
    planner_model: str,
    planner_api_base: str,
    planner_api_key: str,
    router_model: str,
    router_api_base: str,
    router_api_key: str,
    sub_model_api_base: str,
    sub_model_api_key: str,
    pools: PoolConfig,
    planner_temperature: float = 0.7,
    router_temperature: float = 0.3,
) -> dict:
    """Run the full Planner → Router → Executor pipeline (sync)."""
    sub_client = OpenAI(base_url=sub_model_api_base, api_key=sub_model_api_key, timeout=60)

    models_used: list[str] = []
    skills_used: list[str] = []
    routing_decisions: list[dict] = []

    _extra = {"enable_thinking": False} if not _is_local(sub_model_api_base) else {}

    def execute_subtask(instruction: str, task_id: str) -> str:
        selected_model, selected_skill = route_subtask(
            instruction=instruction, model=router_model,
            api_base=router_api_base, api_key=router_api_key,
            pools=pools, temperature=router_temperature,
        )
        models_used.append(selected_model)
        skills_used.append(selected_skill)
        routing_decisions.append({
            "task_id": task_id, "instruction": instruction,
            "routed_model": selected_model, "routed_skill": selected_skill,
        })
        try:
            resp = sub_client.chat.completions.create(
                model=selected_model,
                messages=[{"role": "user", "content": instruction}],
                temperature=0.1, max_tokens=1024,
                extra_body=_extra,
            )
            result = resp.choices[0].message.content.strip()
        except Exception as e:
            result = f"Error: {e}"
        return f"[routed to {selected_model} / {selected_skill}]\n{result}"

    planner_result = run_planner(
        question=question, model=planner_model,
        api_base=planner_api_base, api_key=planner_api_key,
        execute_subtask_fn=execute_subtask, temperature=planner_temperature,
    )
    return _pack(planner_result, models_used, skills_used, routing_decisions)


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------

async def arun_agent(
    question: str,
    planner_model: str,
    planner_api_base: str,
    planner_api_key: str,
    router_model: str,
    router_api_base: str,
    router_api_key: str,
    sub_model_api_base: str,
    sub_model_api_key: str,
    pools: PoolConfig,
    planner_temperature: float = 0.7,
    router_temperature: float = 0.3,
) -> dict:
    """Run the full Planner → Router → Executor pipeline (async)."""
    sub_client = AsyncOpenAI(
        base_url=sub_model_api_base, api_key=sub_model_api_key, timeout=60,
    )

    models_used: list[str] = []
    skills_used: list[str] = []
    routing_decisions: list[dict] = []

    _extra_async = {"enable_thinking": False} if not _is_local(sub_model_api_base) else {}

    async def execute_subtask(instruction: str, task_id: str) -> str:
        selected_model, selected_skill = await aroute_subtask(
            instruction=instruction, model=router_model,
            api_base=router_api_base, api_key=router_api_key,
            pools=pools, temperature=router_temperature,
        )
        models_used.append(selected_model)
        skills_used.append(selected_skill)
        routing_decisions.append({
            "task_id": task_id, "instruction": instruction,
            "routed_model": selected_model, "routed_skill": selected_skill,
        })
        try:
            resp = await sub_client.chat.completions.create(
                model=selected_model,
                messages=[{"role": "user", "content": instruction}],
                temperature=0.1, max_tokens=1024,
                extra_body=_extra_async,
            )
            result = resp.choices[0].message.content.strip()
        except Exception as e:
            result = f"Error: {e}"
        return f"[routed to {selected_model} / {selected_skill}]\n{result}"

    planner_result = await arun_planner(
        question=question, model=planner_model,
        api_base=planner_api_base, api_key=planner_api_key,
        execute_subtask_fn=execute_subtask, temperature=planner_temperature,
    )
    return _pack(planner_result, models_used, skills_used, routing_decisions)


# ---------------------------------------------------------------------------
# Batch (concurrent)
# ---------------------------------------------------------------------------

async def arun_agent_batch(
    questions: Sequence[str],
    planner_model: str,
    planner_api_base: str,
    planner_api_key: str,
    router_model: str,
    router_api_base: str,
    router_api_key: str,
    sub_model_api_base: str,
    sub_model_api_key: str,
    pools: PoolConfig,
    planner_temperature: float = 0.7,
    router_temperature: float = 0.3,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[dict]:
    """Run multiple questions concurrently with a semaphore.

    Each sample's internal pipeline is still sequential (planner → router →
    executor), but different samples run in parallel up to *concurrency*.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(question: str) -> dict:
        async with sem:
            return await arun_agent(
                question=question,
                planner_model=planner_model,
                planner_api_base=planner_api_base,
                planner_api_key=planner_api_key,
                router_model=router_model,
                router_api_base=router_api_base,
                router_api_key=router_api_key,
                sub_model_api_base=sub_model_api_base,
                sub_model_api_key=sub_model_api_key,
                pools=pools,
                planner_temperature=planner_temperature,
                router_temperature=router_temperature,
            )

    return await asyncio.gather(*[_one(q) for q in questions])


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------

def run_agent_single(
    question: str, model: str, api_base: str, api_key: str,
    sub_model_api_base: str, sub_model_api_key: str,
    pools: PoolConfig, temperature: float = 0.7,
) -> dict:
    """Single-model mode: same model as both planner and router."""
    return run_agent(
        question=question,
        planner_model=model, planner_api_base=api_base, planner_api_key=api_key,
        router_model=model, router_api_base=api_base, router_api_key=api_key,
        sub_model_api_base=sub_model_api_base, sub_model_api_key=sub_model_api_key,
        pools=pools, planner_temperature=temperature,
    )


def _pack(planner_result, models_used, skills_used, routing_decisions):
    return {
        "messages": planner_result["messages"],
        "answer": planner_result["answer"],
        "complete": planner_result["complete"],
        "n_delegates": len(models_used),
        "models_used": models_used,
        "skills_used": skills_used,
        "routing_decisions": routing_decisions,
        "subtasks": planner_result["subtasks"],
    }
