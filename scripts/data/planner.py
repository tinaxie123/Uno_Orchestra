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
_MAX_IDENTICAL_CALLS = 3  # Break after N identical consecutive subtask calls


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
              temperature: float, with_tools: bool = True) -> ChatOpenAI:
    extra = {"max_tokens": PLANNER_MAX_TOKENS}
    if not _is_local(api_base):
        extra["enable_thinking"] = False
    llm = ChatOpenAI(
        model=model,
        base_url=api_base,
        api_key=api_key or "none",
        temperature=temperature,
        timeout=120,
        model_kwargs={"extra_body": extra},
    )
    return llm.bind_tools(PLANNER_TOOLS) if with_tools else llm


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
    """Fallback: extract tool calls when the model writes them as plain text.

    Handles three formats that weaker models produce:
    1. <tool_call>{"name": "finish", "arguments": {...}}</tool_call>
    2. finish(answer)  /  plan_subtask(instruction, task_id)
    3. Bare JSON: {"name": "finish", "arguments": {...}}
    """
    # --- Format 1: <tool_call> tags ---
    m = re.search(r'<tool_call>\s*(\{.+?)(?:</tool_call>|$)', text, re.DOTALL)
    if m:
        return _try_parse_json_tool(m.group(1).strip())

    # --- Format 2: finish(...) as plain text ---
    # Match finish("72"), finish(72), Finish('72'), FINISH(some text)
    m = re.search(r'\bfinish\(\s*["\']?(.+?)["\']?\s*\)', text, re.IGNORECASE)
    if m:
        answer = m.group(1).strip().strip("\"'")
        return [{"name": "finish", "args": {"answer": answer}, "id": "text_fallback"}]

    # Match plan_subtask("instruction", "task_id") — rare but possible
    m = re.search(
        r'\bplan_subtask\(\s*["\'](.+?)["\']'
        r'(?:\s*,\s*["\'](\w+)["\'])?\s*\)',
        text, re.DOTALL,
    )
    if m:
        instruction = m.group(1).strip()
        task_id = m.group(2) or "t1"
        return [{"name": "plan_subtask",
                 "args": {"instruction": instruction, "task_id": task_id},
                 "id": "text_fallback"}]

    # --- Format 3: bare JSON (may contain nested braces) ---
    m = re.search(r'(\{"name"\s*:\s*"(?:finish|plan_subtask)"[^}]*\{[^}]*\}[^}]*\})', text, re.DOTALL)
    if not m:
        m = re.search(r'(\{"name"\s*:\s*"(?:finish|plan_subtask)"[^}]*\})', text, re.DOTALL)
    if m:
        return _try_parse_json_tool(m.group(1).strip())

    return None


def _try_parse_json_tool(raw: str) -> list[dict] | None:
    """Try to parse a JSON string as a tool call dict."""
    # Fix common issues: unescaped backslashes, raw newlines in strings
    fixed_backslash = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)

    for candidate in [raw, fixed_backslash]:
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

    # Fallback: regex extraction when JSON is malformed (e.g. contains raw
    # newlines, triple-quotes from code blocks, etc.)
    name_m = re.search(r'"name"\s*:\s*"(finish|plan_subtask)"', raw)
    if name_m:
        name = name_m.group(1)
        if name == "finish":
            ans_m = re.search(r'"answer"\s*:\s*"([^"]*)"', raw)
            return [{"name": "finish",
                      "args": {"answer": ans_m.group(1) if ans_m else ""},
                      "id": "text_fallback"}]
        if name == "plan_subtask":
            # Extract instruction: grab everything between "instruction": " and the next ", "task_id"
            instr_m = re.search(
                r'"instruction"\s*:\s*"(.*?)(?:"\s*,\s*"task_id"|"\s*\})',
                raw, re.DOTALL,
            )
            tid_m = re.search(r'"task_id"\s*:\s*"(\w+)"', raw)
            instruction = instr_m.group(1) if instr_m else raw[name_m.end():][:500]
            # Unescape basic sequences
            instruction = instruction.replace('\\n', '\n').replace('\\t', '\t')
            return [{"name": "plan_subtask",
                      "args": {"instruction": instruction,
                               "task_id": tid_m.group(1) if tid_m else "t1"},
                      "id": "text_fallback"}]

    return None


_NUDGE = HumanMessage(
    content="You MUST take an action now. Either:\n"
            "- Call plan_subtask(instruction, task_id) to delegate work, OR\n"
            "- Call finish(answer) with the final answer.\n"
            "Do NOT respond with empty text.",
)

_ERROR_NUDGE = HumanMessage(
    content="The previous sub-task returned an error. Do NOT repeat the same call. "
            "Either: (1) try a different approach, (2) solve it yourself, or "
            "(3) call finish(answer) with your best answer.",
)

# How many consecutive empty responses before we switch to no-tools fallback
_MAX_EMPTY_BEFORE_FREEFORM = 2

_FREEFORM_PROMPT = HumanMessage(
    content="Solve the problem above step by step. "
            "At the end, write your final answer on its own line as: "
            "ANSWER: <value>",
)

def _extract_answer_from_text(text: str) -> str | None:
    """Try to extract a final answer from free-form model text.

    Looks for explicit markers first, then falls back to the last
    number/expression on its own line.
    """
    # Explicit marker: ANSWER: xxx  or  answer: xxx  or  The answer is xxx
    m = re.search(
        r'(?:ANSWER|answer|Answer|the answer is)[:\s]+(.+?)(?:\n|$)', text,
    )
    if m:
        return m.group(1).strip().rstrip(".")

    # Fallback: last standalone number/expression line
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if re.fullmatch(r'[-+]?\d[\d,./]*', line):
            return line
    return None


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
    system_prompt: str | None = None,
) -> dict:
    llm = _make_llm(model, api_base, api_key, temperature)
    messages: list = [
        SystemMessage(content=system_prompt or build_system_prompt("planner")),
        HumanMessage(content=question),
    ]
    answer, complete, subtasks = None, False, []
    empty_streak = 0
    _recent_calls: list[str] = []

    for step in range(MAX_STEPS):
        try:
            ai_msg: AIMessage = llm.invoke(messages)
        except Exception as e:
            err_str = str(e)
            logger.warning("[Planner] API error (step %d): %s", step, err_str[:200])
            if "validation error" in err_str:
                logger.info("[Planner] Validation error at step %d, trying freeform", step)
                answer, complete = _freeform_fallback_sync(
                    model, api_base, api_key, temperature, messages)
                if complete:
                    break
                continue
            break

        messages.append(ai_msg)

        if ai_msg.tool_calls:
            empty_streak = 0
            for tc in ai_msg.tool_calls:
                name = tc["name"]
                args = tc["args"]

                if name == "finish":
                    answer = args.get("answer", "")
                    if not answer or not answer.strip():
                        logger.warning("[Planner] Empty finish at step %d, nudging", step)
                        messages.append(ToolMessage(
                            content='{"status": "error", "reason": "answer is empty — provide a non-empty answer"}',
                            tool_call_id=tc["id"],
                        ))
                        break
                    complete = True
                    messages.append(ToolMessage(
                        content='{"status": "done"}', tool_call_id=tc["id"],
                    ))
                    break
                elif name == "plan_subtask":
                    instruction = args.get("instruction", "")
                    task_id = args.get("task_id", f"t{len(subtasks) + 1}")

                    call_sig = instruction[:200]
                    _recent_calls.append(call_sig)
                    identical_count = sum(1 for c in _recent_calls if c == call_sig)
                    if identical_count >= _MAX_IDENTICAL_CALLS:
                        logger.warning("[Planner] Loop detected at step %d", step)
                        messages.append(ToolMessage(
                            content='{"status": "error", "reason": "Loop detected — solve it yourself or call finish."}',
                            tool_call_id=tc["id"],
                        ))
                        break

                    result = execute_subtask_fn(instruction, task_id)
                    subtasks.append({"task_id": task_id, "instruction": instruction, "result": result})

                    if "Error:" in result and ("500" in result or "error" in result.lower()):
                        logger.warning("[Planner] Subtask error at step %d", step)
                        messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
                        messages.append(_ERROR_NUDGE)
                        break

                    messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

            if complete:
                break
            continue

        text = (ai_msg.content or "").strip()
        if not text:
            empty_streak += 1
            logger.warning("[Planner] Empty response at step %d (streak=%d)", step, empty_streak)
            if empty_streak >= _MAX_EMPTY_BEFORE_FREEFORM:
                answer, complete = _freeform_fallback_sync(
                    model, api_base, api_key, temperature, messages)
                if complete:
                    break
                break
            messages.append(_NUDGE)
            continue

        empty_streak = 0
        parsed = _parse_text_tool_call(text)
        if parsed:
            logger.info("[Planner] Parsed tool call from plain text at step %d", step)
            for tc in parsed:
                if tc["name"] == "finish":
                    answer = tc["args"].get("answer", "")
                    if answer and answer.strip():
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

        extracted = _extract_answer_from_text(text)
        if extracted:
            logger.info("[Planner] Extracted answer from plain text at step %d", step)
            answer = extracted
            complete = True
            break

        logger.info("[Planner] Plain text at step %d (no answer found)", step)

    # ── Fix 3: last-resort — extract from subtask results if planner never finished ──
    if not complete and subtasks:
        for st in reversed(subtasks):
            res = st.get("result", "")
            extracted = _extract_answer_from_text(res)
            if extracted:
                logger.info("[Planner] Last-resort: extracted answer from subtask %s", st["task_id"])
                answer = extracted
                complete = True
                break
    if not complete and not answer:
        answer, complete = _freeform_fallback_sync(
            model, api_base, api_key, temperature, messages)

    return _build_result(messages, answer, complete, subtasks)


def _freeform_fallback_sync(model, api_base, api_key, temperature, messages):
    """Retry without tools — let the model answer in free-form text."""
    logger.info("[Planner] Freeform fallback: retrying without tools")
    llm_no_tools = _make_llm(model, api_base, api_key, temperature, with_tools=False)
    # Build a clean 2-message prompt: system + question (skip the failed tool attempts)
    clean_msgs = [m for m in messages[:2]]  # system + user
    clean_msgs.append(_FREEFORM_PROMPT)
    try:
        ai_msg = llm_no_tools.invoke(clean_msgs)
        text = (ai_msg.content or "").strip()
        messages.append(ai_msg)
        if text:
            # Try text tool-call parse first, then raw extraction
            parsed = _parse_text_tool_call(text)
            if parsed and parsed[0]["name"] == "finish":
                answer = parsed[0]["args"].get("answer", "")
                logger.info("[Planner] Freeform fallback: parsed finish(%s)", answer)
                return answer, True
            extracted = _extract_answer_from_text(text)
            if extracted:
                logger.info("[Planner] Freeform fallback: extracted '%s'", extracted)
                return extracted, True
    except Exception as e:
        logger.warning("[Planner] Freeform fallback error: %s", e)
    return None, False


async def _freeform_fallback_async(model, api_base, api_key, temperature, messages):
    """Async version of freeform fallback."""
    logger.info("[Planner] Freeform fallback: retrying without tools")
    llm_no_tools = _make_llm(model, api_base, api_key, temperature, with_tools=False)
    clean_msgs = [m for m in messages[:2]]
    clean_msgs.append(_FREEFORM_PROMPT)
    try:
        ai_msg = await llm_no_tools.ainvoke(clean_msgs)
        text = (ai_msg.content or "").strip()
        messages.append(ai_msg)
        if text:
            parsed = _parse_text_tool_call(text)
            if parsed and parsed[0]["name"] == "finish":
                answer = parsed[0]["args"].get("answer", "")
                logger.info("[Planner] Freeform fallback: parsed finish(%s)", answer)
                return answer, True
            extracted = _extract_answer_from_text(text)
            if extracted:
                logger.info("[Planner] Freeform fallback: extracted '%s'", extracted)
                return extracted, True
    except Exception as e:
        logger.warning("[Planner] Freeform fallback error: %s", e)
    return None, False


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
    system_prompt: str | None = None,
) -> dict:
    """Async version — execute_subtask_fn must be an async callable."""
    llm = _make_llm(model, api_base, api_key, temperature)
    messages: list = [
        SystemMessage(content=system_prompt or build_system_prompt("planner")),
        HumanMessage(content=question),
    ]
    answer, complete, subtasks = None, False, []
    empty_streak = 0
    _recent_calls: list[str] = []  # Track recent plan_subtask calls for loop detection

    for step in range(MAX_STEPS):
        # ── Fix 2: catch validation errors (bad tool_calls format) and fallback parse ──
        try:
            ai_msg: AIMessage = await llm.ainvoke(messages)
        except Exception as e:
            err_str = str(e)
            logger.warning("[Planner] API error (step %d): %s", step, err_str[:200])
            # If validation error (e.g. tool_calls.0.args is str not dict),
            # try freeform fallback instead of giving up immediately
            # Validation error = bad tool_calls format from vLLM.
            # Nudge won't help (model repeats same bad format).
            # Go straight to freeform fallback.
            if "validation error" in err_str:
                logger.info("[Planner] Validation error at step %d, trying freeform", step)
                answer, complete = await _freeform_fallback_async(
                    model, api_base, api_key, temperature, messages)
                if complete:
                    break
                # freeform failed too — continue loop in case next attempt works
                continue
            break

        messages.append(ai_msg)

        if ai_msg.tool_calls:
            empty_streak = 0
            for tc in ai_msg.tool_calls:
                name = tc["name"]
                args = tc["args"]

                if name == "finish":
                    answer = args.get("answer", "")
                    # ── Empty answer guard ──
                    if not answer or not answer.strip():
                        logger.warning("[Planner] Empty finish at step %d, nudging", step)
                        messages.append(ToolMessage(
                            content='{"status": "error", "reason": "answer is empty — provide a non-empty answer"}',
                            tool_call_id=tc["id"],
                        ))
                        break
                    complete = True
                    messages.append(ToolMessage(
                        content='{"status": "done"}', tool_call_id=tc["id"],
                    ))
                    break
                elif name == "plan_subtask":
                    instruction = args.get("instruction", "")
                    task_id = args.get("task_id", f"t{len(subtasks) + 1}")

                    # ── Loop detection ──
                    call_sig = instruction[:200]
                    _recent_calls.append(call_sig)
                    identical_count = sum(1 for c in _recent_calls if c == call_sig)
                    if identical_count >= _MAX_IDENTICAL_CALLS:
                        logger.warning("[Planner] Loop detected at step %d: %d identical calls", step, identical_count)
                        messages.append(ToolMessage(
                            content='{"status": "error", "reason": "Loop detected — you repeated the same subtask too many times. Solve it yourself or call finish with your best answer."}',
                            tool_call_id=tc["id"],
                        ))
                        break

                    result = await execute_subtask_fn(instruction, task_id)
                    subtasks.append({
                        "task_id": task_id,
                        "instruction": instruction,
                        "result": result,
                    })

                    # ── Error detection: nudge planner to change strategy ──
                    if "Error:" in result and ("500" in result or "error" in result.lower()):
                        logger.warning("[Planner] Subtask returned error at step %d", step)
                        messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
                        messages.append(_ERROR_NUDGE)
                        break

                    messages.append(ToolMessage(
                        content=result, tool_call_id=tc["id"],
                    ))

            if complete:
                break
            continue

        # ── Fix 1: empty response — strong nudge with mandatory action ──
        text = (ai_msg.content or "").strip()
        if not text:
            empty_streak += 1
            logger.warning("[Planner] Empty response at step %d (streak=%d)", step, empty_streak)
            if empty_streak >= _MAX_EMPTY_BEFORE_FREEFORM:
                answer, complete = await _freeform_fallback_async(
                    model, api_base, api_key, temperature, messages)
                if complete:
                    break
                break
            messages.append(_NUDGE)
            continue

        empty_streak = 0
        # ── Fix 2: fallback parse for plain-text tool calls / truncated JSON ──
        parsed = _parse_text_tool_call(text)
        if parsed:
            logger.info("[Planner] Parsed tool call from plain text at step %d", step)
            for tc in parsed:
                if tc["name"] == "finish":
                    answer = tc["args"].get("answer", "")
                    if answer and answer.strip():
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

        # Plain text with reasoning but no finish — try to extract answer
        extracted = _extract_answer_from_text(text)
        if extracted:
            logger.info("[Planner] Extracted answer from plain text at step %d", step)
            answer = extracted
            complete = True
            break

        logger.info("[Planner] Plain text at step %d (no answer found)", step)

    # ── Fix 3: last-resort — extract from subtask results if planner never finished ──
    if not complete and subtasks:
        # Try to salvage an answer from the last subtask result
        for st in reversed(subtasks):
            res = st.get("result", "")
            extracted = _extract_answer_from_text(res)
            if extracted:
                logger.info("[Planner] Last-resort: extracted answer from subtask %s", st["task_id"])
                answer = extracted
                complete = True
                break
    if not complete and not answer:
        # Final freeform attempt if we have no answer at all
        answer, complete = await _freeform_fallback_async(
            model, api_base, api_key, temperature, messages)

    return _build_result(messages, answer, complete, subtasks)
