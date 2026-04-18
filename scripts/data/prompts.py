from __future__ import annotations

from typing import Literal

from configs import PoolConfig

PLANNER_SYSTEM_PROMPT = """You are a task planner. For each user query, choose one action:
(A) finish directly — for simple, single-step tasks (PREFER THIS for straightforward questions)
(B) one subtask then finish — only if the task truly needs a specialist
(C) multi-subtask decomposition — only for genuinely multi-step problems

Tools:
- plan_subtask(instruction, task_id): delegate to a specialist worker
- finish(answer): provide final answer

Rules:
1. PREFER fewer steps. Most math problems need at most ONE subtask. Do NOT over-decompose.
2. Each instruction MUST be fully self-contained. The worker has NO access to the original question.
   - Copy ALL numbers, names, and conditions into the instruction verbatim.
   - NEVER use phrases like "the given equations", "from the problem", "as described above".
3. If a worker says "I need more info", you MUST rewrite the instruction with the missing data included. Do NOT create a new subtask instead.
4. Stop when you have enough evidence and call finish.
5. finish(answer) must contain ONLY the final answer. No words, no units, no explanation.
   - You MUST evaluate any arithmetic yourself BEFORE calling finish — NEVER pass an unevaluated expression.
   - Multiple-choice questions (options labeled A/B/C/D/E): return the LETTER only, NOT the computed value.
   - Proposition/statement selection (e.g. ①②③④): return the ORIGINAL symbols exactly, NOT digits.
   - Interval/set answers: return the full interval notation, NOT a single number.
   - All other questions: return a single computed number or simplified value.

"""

ROUTER_SYSTEM_PROMPT_TEMPLATE = """You are a routing agent. Given one sub-task instruction, select the best (model, skill) pair that balances cost and quality.

Available models (cheapest first):
{model_lines}

Available skills:
{skill_lines}

Rules:
- Use only one model and one skill.
- PREFER cheaper models when they can handle the task. Do NOT default to the most expensive model.
- A $3 model that can solve the task is ALWAYS better than a $15 model.
- Only escalate to expensive models (gpt-5.3-codex, gpt-5.4, claude-*) for genuinely hard problems.
- Ensure the selected skill is in the model's allowed skill list.
- Return your decision via the route tool call."""


def build_system_prompt(role: Literal["planner", "router"], pools: PoolConfig | None = None) -> str:
    """Unified prompt builder for all agent roles."""
    if role == "planner":
        return PLANNER_SYSTEM_PROMPT

    if pools is None:
        raise ValueError("pools is required when role='router'")

    raw_models = pools["raw"]["models"]
    model_by_id = {m["id"]: m for m in raw_models}
    model_lines = []
    for model_id, cost in sorted(pools["cost_per_m"].items(), key=lambda item: item[1]):
        m = model_by_id.get(model_id, {})
        desc = m.get("description", "").strip().replace("\n", " ")
        skills = ", ".join(pools["model_skills"].get(model_id, []))
        model_lines.append(f"  {model_id} (${cost:.2f}/1M output)\n    {desc}\n    Skills: [{skills}]")

    raw_skills = pools["raw"].get("skills", [])
    skill_lines = []
    for s in raw_skills:
        if isinstance(s, dict):
            sid = s["id"]
            sdesc = s.get("description", "")
            skill_lines.append(f"  {sid}: {sdesc}")
        else:
            skill_lines.append(f"  {s}")

    return ROUTER_SYSTEM_PROMPT_TEMPLATE.format(
        model_lines="\n".join(model_lines),
        skill_lines="\n".join(skill_lines),
    )

