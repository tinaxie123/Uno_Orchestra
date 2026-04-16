"""Agent execution: multi-turn tool-calling loop for router and teacher.

This module handles the core interaction between the orchestrator (router or teacher)
and sub-models via the delegate_task / finish tool protocol.
"""

from __future__ import annotations

import json

from configs import PoolConfig

MAX_STEPS = 8


def build_system_prompt(pools: PoolConfig) -> str:
    model_list = ", ".join(pools["models"])
    skill_list = ", ".join(pools["skills"])
    skill_lines = []
    for mid, skills in sorted(pools["model_skills"].items()):
        skill_lines.append(f"  {mid}: [{', '.join(skills)}]")

    return f"""You are a task orchestrator that solves problems by delegating sub-tasks to specialized models.

Available models: {model_list}
Available skills: {skill_list}

Per-model allowed skills:
{chr(10).join(skill_lines)}

You have two tools:
- delegate_task(instruction, model, skill): delegate a sub-task to a model with a specific skill
- finish(answer): provide your final answer

Think step by step. Pick the cheapest model that can handle each sub-task. When you have enough information, call finish."""


def build_tools(pools: PoolConfig) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "delegate_task",
            "description": "Delegate a sub-task to a specialized model",
            "parameters": {"type": "object", "properties": {
                "instruction": {"type": "string", "description": "What the model should do"},
                "model": {"type": "string", "enum": pools["models"]},
                "skill": {"type": "string", "enum": pools["skills"]},
            }, "required": ["instruction", "model", "skill"]},
        }},
        {"type": "function", "function": {
            "name": "finish",
            "description": "Provide the final answer",
            "parameters": {"type": "object", "properties": {
                "answer": {"type": "string"},
            }, "required": ["answer"]},
        }},
    ]


def run_agent(
    question: str,
    model: str,
    api_base: str,
    api_key: str,
    sub_model_api_base: str,
    sub_model_api_key: str,
    pools: PoolConfig,
    temperature: float = 0.3,
) -> dict:
    """Run a multi-turn agent loop: LLM -> tool call -> sub-model -> return.

    Returns dict with keys: messages, answer, complete, n_delegates, models_used, skills_used.
    """
    from openai import OpenAI

    client = OpenAI(base_url=api_base, api_key=api_key, timeout=120)
    sub_client = OpenAI(base_url=sub_model_api_base, api_key=sub_model_api_key, timeout=60)
    tools = build_tools(pools)

    messages = [
        {"role": "system", "content": build_system_prompt(pools)},
        {"role": "user", "content": question},
    ]

    n_delegates = 0
    answer = None
    complete = False
    models_used = []
    skills_used = []

    for _ in range(MAX_STEPS):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                temperature=temperature, max_tokens=2048,
            )
        except Exception:
            break

        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append(msg.model_dump())
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)

                if name == "finish":
                    answer = args.get("answer", "")
                    complete = True
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps({"status": "done"})})
                    break
                elif name == "delegate_task":
                    n_delegates += 1
                    models_used.append(args.get("model", pools["models"][0]))
                    skills_used.append(args.get("skill", "direct_answer"))
                    try:
                        sub_resp = sub_client.chat.completions.create(
                            model=args["model"],
                            messages=[{"role": "user", "content": args["instruction"]}],
                            temperature=0.1, max_tokens=1024,
                        )
                        result = sub_resp.choices[0].message.content.strip()
                    except Exception as e:
                        result = f"Error: {e}"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if complete:
                break
        elif msg.content:
            messages.append({"role": "assistant", "content": msg.content})
        else:
            break

    return {
        "messages": messages,
        "answer": answer,
        "complete": complete,
        "n_delegates": n_delegates,
        "models_used": models_used,
        "skills_used": skills_used,
    }
