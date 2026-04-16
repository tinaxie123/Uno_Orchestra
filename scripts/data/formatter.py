"""Output formatting: convert agent trajectories to ShareGPT format for LlamaFactory."""

from __future__ import annotations

import json

MAX_TRAINING_TOKENS = 8192


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(msg.get("content", ""))) for msg in messages) // 3


def to_sharegpt(messages: list[dict]) -> list[dict]:
    """Convert OpenAI messages format to ShareGPT format.

    Mapping:
        system    -> {"from": "system",        "value": ...}
        user      -> {"from": "human",         "value": ...}
        assistant -> {"from": "gpt",           "value": ...}
        tool_call -> {"from": "function_call", "value": ...}
        tool      -> {"from": "observation",   "value": ...}
    """
    conversations = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            conversations.append({"from": "system", "value": msg["content"]})
        elif role == "user":
            conversations.append({"from": "human", "value": msg["content"]})
        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                if msg.get("content"):
                    conversations.append({"from": "gpt", "value": msg["content"]})
                for tc in tool_calls:
                    fn = tc if isinstance(tc, dict) else tc
                    call = {
                        "name": fn["function"]["name"],
                        "arguments": (
                            json.loads(fn["function"]["arguments"])
                            if isinstance(fn["function"]["arguments"], str)
                            else fn["function"]["arguments"]
                        ),
                    }
                    conversations.append({
                        "from": "function_call",
                        "value": json.dumps(call, ensure_ascii=False),
                    })
            else:
                conversations.append({"from": "gpt", "value": msg.get("content", "")})
        elif role == "tool":
            conversations.append({"from": "observation", "value": msg.get("content", "")})
    return conversations


def filter_and_pack(task: dict, teacher_result: dict) -> tuple[bool, dict | None]:
    """Stage 3: discard overlong trajectories, pack surviving ones as ShareGPT."""
    est = estimate_tokens(teacher_result["messages"])
    if est > MAX_TRAINING_TOKENS:
        return False, None
    return True, {
        "conversations": to_sharegpt(teacher_result["messages"]),
        "source": task["source"],
        "domain": task["domain"],
        "gold_answer": task["gold_answer"],
        "n_delegates": teacher_result["n_delegates"],
        "models_used": teacher_result["models_used"],
        "skills_used": teacher_result["skills_used"],
    }
