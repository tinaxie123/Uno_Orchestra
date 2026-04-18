"""Planner: task decomposition and orchestration via LangChain.

Uses LangChain's tool-calling abstraction so the planner works reliably
with any model backend (vLLM, OpenAI, etc.) regardless of native
tool-call support — LangChain handles format conversion internally.

Provides both sync (run_planner) and async (arun_planner) entry points.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable, Union

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from scripts.data.prompts import build_system_prompt

logger = logging.getLogger(__name__)

MAX_STEPS = 8
PLANNER_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# Tools (LangChain @tool declarations)
# ---------------------------------------------------------------------------

@tool
def plan_subtask(instruction: str, task_id: str) -> str:
    """Create a sub-task to be executed by a specialist.

    Args:
        instruction: Clear, self-contained instruction. Include all data the specialist needs.
        task_id: Unique label, e.g. t1, t2.
    """
    raise NotImplementedError


@tool
def finish(answer: str) -> str:
    """Provide the final answer.

    Args:
        answer: The final value — a number, expression, or choice letter. No explanation.
    """
    raise NotImplementedError


PLANNER_TOOLS = [plan_subtask, finish]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_local(api_base: str) -> bool:
    return "localhost" in api_base or "127.0.0.1" in api_base


def _make_llm(model: str, api_base: str, api_key: str,
              temperature: float) -> ChatOpenAI:
    extra = {"max_tokens": PLANNER_MAX_TOKENS}
    if not _is_local(api_base):
        extra["enable_thinking"] = False
    return ChatOpenAI(
        model=model,
        base_url=api_base,
        api_key=api_key or "none",
        temperature=temperature,
        timeout=120,
        model_kwargs={"extra_body": extra},
    ).bind_tools(PLANNER_TOOLS)


def _handle_tool_calls(ai_msg, execute_fn_result, subtasks, messages):
    """Process tool calls from an AI message. Returns (answer, complete)."""
    answer = None
    complete = False
    for tc in ai_msg.tool_calls:
        name = tc["name"]
        args = tc["args"]

        if name == "finish":
            answer = args.get("answer", "")
            complete = True
            messages.append(ToolMessage(
                content='{"status": "done"}', tool_call_id=tc["id"],
            ))
            break

        elif name == "plan_subtask":
            instruction = args.get("instruction", "")
            task_id = args.get("task_id", f"t{len(subtasks) + 1}")

            subtasks.append({
                "task_id": task_id,
                "instruction": instruction,
                "result": execute_fn_result(instruction, task_id),
            })
            messages.append(ToolMessage(
                content=subtasks[-1]["result"], tool_call_id=tc["id"],
            ))

    return answer, complete


def _serialise_messages(messages: list) -> list[dict]:
    """Convert LangChain message objects to plain dicts for JSON storage."""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            entry: dict = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["args"]}}
                    for tc in m.tool_calls
                ]
            out.append(entry)
        elif isinstance(m, ToolMessage):
            out.append({
                "role": "tool", "tool_call_id": m.tool_call_id,
                "content": m.content,
            })
        else:
            out.append({"role": "unknown", "content": str(m.content)})
    return out


def _parse_text_tool_call(text: str) -> list[dict] | None:
    """Fallback: parse <tool_call> tags when vLLM returns them as plain text."""
    m = re.search(r'<tool_call>\s*(\{.+?)(?:</tool_call>|$)', text, re.DOTALL)
    if not m:
        return None
    raw = m.group(1).strip()
    # Fix LaTeX backslashes for JSON parsing
    fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
    for candidate in [raw, fixed]:
        try:
            obj = json.loads(candidate)
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name in ("finish", "plan_subtask"):
                return [{"name": name, "args": args, "id": "text_fallback"}]
        except json.JSONDecodeError:
            continue
    return None


_NUDGE = HumanMessage(
    content="Now call finish(answer) with the final value, "
            "or plan_subtask() to continue.",
)


def _build_result(messages, answer, complete, subtasks):
    return {
        "messages": _serialise_messages(messages),
        "answer": answer,
        "complete": complete,
        "subtasks": subtasks,
    }


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

def run_planner(
    question: str,
    model: str,
    api_base: str,
    api_key: str,
    execute_subtask_fn: Callable[[str, str], str],
    temperature: float = 0.7,
) -> dict:
    llm = _make_llm(model, api_base, api_key, temperature)
    messages: list = [
        SystemMessage(content=build_system_prompt("planner")),
        HumanMessage(content=question),
    ]
    answer, complete, subtasks = None, False, []

    for step in range(MAX_STEPS):
        try:
            ai_msg: AIMessage = llm.invoke(messages)
        except Exception as e:
            logger.warning("[Planner] API error (step %d): %s", step, e)
            break

        messages.append(ai_msg)

        if ai_msg.tool_calls:
            answer, complete = _handle_tool_calls(
                ai_msg, execute_subtask_fn, subtasks, messages)
            if complete:
                break
            continue

        text = (ai_msg.content or "").strip()
        if not text:
            logger.warning("[Planner] Empty response at step %d", step)
            messages.append(_NUDGE)
            continue

        # Fallback: vLLM sometimes returns <tool_call> as plain text
        parsed = _parse_text_tool_call(text)
        if parsed:
            logger.info("[Planner] Parsed tool call from plain text at step %d", step)
            for tc in parsed:
                if tc["name"] == "finish":
                    answer = tc["args"].get("answer", "")
                    complete = True
                    break
                elif tc["name"] == "plan_subtask":
                    instruction = tc["args"].get("instruction", "")
                    task_id = tc["args"].get("task_id", f"t{len(subtasks) + 1}")
                    if instruction:
                        result = execute_subtask_fn(instruction, task_id)
                        subtasks.append({"task_id": task_id, "instruction": instruction, "result": result})
                        messages.append(HumanMessage(content=result))
            if complete:
                break
            continue

        logger.info("[Planner] Plain text at step %d", step)

    return _build_result(messages, answer, complete, subtasks)


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------

async def arun_planner(
    question: str,
    model: str,
    api_base: str,
    api_key: str,
    execute_subtask_fn: Callable[[str, str], Awaitable[str]],
    temperature: float = 0.7,
) -> dict:
    """Async version — execute_subtask_fn must be an async callable."""
    llm = _make_llm(model, api_base, api_key, temperature)
    messages: list = [
        SystemMessage(content=build_system_prompt("planner")),
        HumanMessage(content=question),
    ]
    answer, complete, subtasks = None, False, []

    for step in range(MAX_STEPS):
        try:
            ai_msg: AIMessage = await llm.ainvoke(messages)
        except Exception as e:
            logger.warning("[Planner] API error (step %d): %s", step, e)
            break

        messages.append(ai_msg)

        if ai_msg.tool_calls:
            for tc in ai_msg.tool_calls:
                name = tc["name"]
                args = tc["args"]

                if name == "finish":
                    answer = args.get("answer", "")
                    complete = True
                    messages.append(ToolMessage(
                        content='{"status": "done"}', tool_call_id=tc["id"],
                    ))
                    break
                elif name == "plan_subtask":
                    instruction = args.get("instruction", "")
                    task_id = args.get("task_id", f"t{len(subtasks) + 1}")
                    result = await execute_subtask_fn(instruction, task_id)
                    subtasks.append({
                        "task_id": task_id,
                        "instruction": instruction,
                        "result": result,
                    })
                    messages.append(ToolMessage(
                        content=result, tool_call_id=tc["id"],
                    ))

            if complete:
                break
            continue

        text = (ai_msg.content or "").strip()
        if not text:
            logger.warning("[Planner] Empty response at step %d", step)
            messages.append(_NUDGE)
            continue

        # Fallback: vLLM sometimes returns <tool_call> as plain text
        parsed = _parse_text_tool_call(text)
        if parsed:
            logger.info("[Planner] Parsed tool call from plain text at step %d", step)
            for tc in parsed:
                if tc["name"] == "finish":
                    answer = tc["args"].get("answer", "")
                    complete = True
                    break
                elif tc["name"] == "plan_subtask":
                    instruction = tc["args"].get("instruction", "")
                    task_id = tc["args"].get("task_id", f"t{len(subtasks) + 1}")
                    if instruction:
                        result = await execute_subtask_fn(instruction, task_id)
                        subtasks.append({"task_id": task_id, "instruction": instruction, "result": result})
                        messages.append(HumanMessage(content=result))
            if complete:
                break
            continue

        logger.info("[Planner] Plain text at step %d", step)

    return _build_result(messages, answer, complete, subtasks)
