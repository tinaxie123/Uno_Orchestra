"""
SFT distillation pipeline for schema trajectories.

Pulls (query, gold) pairs from datasets listed in config/sft_recipe.yaml,
calls a teacher model (claude-opus-4-6 or claude-sonnet-4-6) via an
OpenAI-compatible endpoint, parses the teacher output into a multi-turn
ChatML messages list that complies with schema, validates the result
via scripts/validate_schema.py, retries on failure, and appends the final
sample to a JSONL output file.

Usage
-----
    # Smoke test: distill ONE sample from one dataset
    python3 scripts/generate_trajectories.py --smoke

    # Phase A5 dry run (30 stratified samples)
    python3 scripts/generate_trajectories.py --dryrun

    # Distill 5 samples from one dataset
    python3 scripts/generate_trajectories.py --only nq_open --n 5

Environment
-----------
    XIAOJING_API_KEY  : the OpenAI-compatible API key
    XIAOJING_BASE_URL : default https://open.xiaojingai.com/v1/

Outputs
-------
    data/sft/dryrun.jsonl       : one sample per line, includes messages + stats
    data/sft/dryrun_summary.json: aggregate counts, cost, valid rate

Schema enforcement
-----------------------
The teacher prompt enumerates schema §2 tags, §3 rules, the closed
worker pool from config/pools.yaml, and three canonical exemplars from
schema §6. Every teacher output is run through validate_messages();
invalid samples are retried up to 3 times with the validator's error list
appended to the prompt. Samples that never validate are written to a
separate `*_failed.jsonl` for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Make scripts/ importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_schema import load_pools, validate_messages  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RECIPE_PATH = os.path.join(REPO_ROOT, "config/sft_recipe.yaml")
POOLS_PATH = os.path.join(REPO_ROOT, "config/pools.yaml")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "data/sft")

DEFAULT_BASE_URL = os.environ.get("XIAOJING_BASE_URL", "https://open.xiaojingai.com/v1/")
DEFAULT_MAX_RETRIES = 3
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096

# Phase A5 stratification target counts (sum = 30).
A5_STRATIFICATION = {
    "multihop_qa": 4,
    "single_hop_lazy": 3,
    "math": 3,
    "code": 3,
    "stem": 3,
    "commonsense_social": 3,
    "formal_logic": 2,
    "long_context": 2,
    "domain_knowledge": 2,
    "tool_agent": 3,
}
# total = 28, plus 2 free slots filled from the largest domain
A5_FREE_SLOTS = 2

# Phase B1 stratification target counts (sum = 400). Mirrors the recipe ratios:
# multihop 11000 / single 6000 / math 6000 / code 5000 / stem 4500 / common 4000 /
# logic 1800 / tool 1400 / long 900 / dom 900  -- total 41500
# Scale: 400 / 41500 * domain
B1_STRATIFICATION = {
    "multihop_qa": 106,         # 11000/41500 * 400 ~= 106
    "single_hop_lazy": 58,      # 6000/41500 * 400 ~= 58
    "math": 58,                 # 6000/41500 * 400 ~= 58
    "code": 48,                 # 5000/41500 * 400 ~= 48
    "stem": 43,                 # 4500/41500 * 400 ~= 43
    "commonsense_social": 39,   # 4000/41500 * 400 ~= 39
    "formal_logic": 17,         # 1800/41500 * 400 ~= 17
    "tool_agent": 13,           # 1400/41500 * 400 ~= 13
    "long_context": 9,          # 900/41500 * 400 ~= 9
    "domain_knowledge": 9,      # 900/41500 * 400 ~= 9
}
# Sum = 400 exactly

# Behavioral hints (target distribution)
#   lazy         ~15%  : zero decomposition; <final_answer> directly
#   oneshot      ~50%  : single round, complete plan, verify pass
#   continuation ~30%  : round 1 plan emits the part of the DAG that does NOT
#                        depend on observation; round 2 plan emits subtasks that
#                        only became expressible AFTER seeing round 1's obs.
#                        This is observation-driven decomposition, NOT failure repair.
#   decomp_repair  ~5% : round 1 plan was structurally wrong (bad subtask
#                        boundaries / dependencies); round 2 emits a substantially
#                        different DAG. Used sparingly to keep the repair branch
#                        from dominating the cascade story.
BEHAVIOR_LAZY = "lazy"
BEHAVIOR_ONESHOT = "oneshot"
BEHAVIOR_CONTINUATION = "continuation"
BEHAVIOR_DECOMP_REPAIR = "decomp_repair"

# Per-1M-token costs (USD) for cost meter, calibrated against the
# xiaojingai endpoint pricing page (CNY/USD ≈ 7.16). Approximate.
# These are NOT the upstream provider's official prices (xiaojingai's
# proxy is ~50x cheaper for Anthropic models), so do not reuse this
# table for billing reconciliation against direct API contracts.
COST_TABLE = {
    # Anthropic (verified from xiaojingai pricing page 2026-04-09)
    "claude-opus-4-6":              {"in": 0.314, "out": 1.571},   # ¥2.25/¥11.25
    # NOTE: -thinking variants NOT used. CoT is not a router skill.
    "claude-sonnet-4-6":            {"in": 0.189, "out": 0.943},   # ¥1.35/¥6.75
    "claude-haiku-4-5-20251001":    {"in": 0.063, "out": 0.314},   # ¥0.45/¥2.25
    # OpenAI (rough estimates pending pricing screenshot for these specific variants)
    "gpt-5.4":                      {"in": 0.007, "out": 0.042},   # ¥0.05/¥0.30 (if real)
    "gpt-5.3-codex":                {"in": 0.21,  "out": 1.68},    # ¥1.5/¥12 (compact-equivalent)
    # Google
    "gemini-3.1-pro-preview":       {"in": 0.4,   "out": 2.0},     # estimated
    # Moonshot
    "kimi-k2.5":                    {"in": 0.15,  "out": 0.5},     # estimated
    # Alibaba (closed-source flagship; policy backbone is the OPEN-WEIGHT Qwen2.5-7B)
    "qwen3.6-plus":                 {"in": 0.3,   "out": 1.5},     # estimated
    # Google flash
    "gemini-2.5-flash":             {"in": 0.05,  "out": 0.2},     # estimated
}


# ---------------------------------------------------------------------------
# Teacher prompt construction
# ---------------------------------------------------------------------------


def build_system_prompt(pools: dict) -> str:
    models = ", ".join(pools["available_models"])
    skills = ", ".join(pools["available_skills"])

    # Per-model allowed_skills for the teacher to respect.
    allowed_lines = []
    for mid, allowed in sorted(pools["model_allowed_skills"].items()):
        allowed_lines.append(f"  {mid}: [{', '.join(allowed)}]")
    allowed_block = "\n".join(allowed_lines)

    return f"""You are generating ONE training trajectory for a hierarchical agent that decomposes a question into subtasks and routes each to a (model, skill) pair. The trajectory MUST follow schema EXACTLY.

# Available worker models (closed vocab)
{models}

# Available skills (closed vocab)
{skills}

# Per-model allowed skills (you MUST respect this whitelist)
{allowed_block}

# Schema tags (use EXACTLY these)
- <plan round="N">...</plan>     : container for one decomposition round (N=1 for initial, 2/3 for repair)
- <subtask id="K" depends_on="i,j">...</subtask>  : ONE node in the task DAG. depends_on is ALWAYS present (use depends_on="" if no deps). ids are global, strictly increasing, never reused. Multi-id depends_on must be ascending: depends_on="1,2".
- <route round="N" subtask="K" model="MODEL_ID" skill="SKILL_ID">subtask description</route>
- <obs subtask="K">tool result</obs>     : MUST be inside a "tool" turn, never an assistant turn
- <verify round="N" status="STATUS" target="i,j">reason</verify>  : status is "pass" or "repair_needed". target is REQUIRED iff status="repair_needed"; ascending list of subtask ids that need repair.
- <final_answer>...</final_answer>  : exactly one, in the LAST assistant turn

# Hard rules (any violation = invalid sample)
1. Exactly ONE <final_answer>, in the last assistant turn.
2. The first assistant turn is either:
   (a) lazy mode: <final_answer> directly, no <plan>, no <route>; OR
   (b) plan mode: <plan round="1"> + at least one <route round="1">.
3. <plan> rounds strictly increasing 1, 2, 3, ...; round="1" appears at most once.
4. <subtask id> is globally strictly increasing across rounds; never reuse.
5. depends_on always present (use ""); references must be already-declared subtasks; no cycles; declared earlier in this or a previous plan.
6. Every routed subtask receives EXACTLY ONE <obs> with the matching id.
7. route.round MUST equal the round of the <plan> containing the referenced subtask.
8. <verify round="N"> appears immediately after all <obs> of round N.
9. <verify status="pass"> MUST be followed by <final_answer> (no further plan).
10. <verify status="repair_needed"> MUST be followed by <plan round="N+1">. NEVER directly by <final_answer>.
11. Total <route> count across all rounds <= 8. Total <plan> rounds <= 3.
12. model and skill must be in the closed vocab AND in the per-model allowed_skills list above.

# Output format you MUST emit
Output a single text block. Use [ASSISTANT] and [TOOL] markers to separate turns:

[ASSISTANT]
(assistant content here, can contain <plan>, <route>, <verify>, <final_answer>)
[/ASSISTANT]
[TOOL]
(tool content here, ONLY <obs> tags)
[/TOOL]
[ASSISTANT]
(more assistant content)
[/ASSISTANT]
... and so on. The first turn is ALWAYS assistant. The last turn is ALWAYS assistant containing <final_answer>.

# Three canonical exemplars (study them carefully)

## Exemplar 1: lazy mode (zero decomposition)
[ASSISTANT]
<final_answer>Paris</final_answer>
[/ASSISTANT]

## Exemplar 2: one-shot success (4 subtasks with dependencies)
[ASSISTANT]
<plan round="1">
  <subtask id="1" depends_on="">Identify the country whose capital is Lima</subtask>
  <subtask id="2" depends_on="1">Find that country's 2023 GDP</subtask>
  <subtask id="3" depends_on="1">Find that country's 2023 population</subtask>
  <subtask id="4" depends_on="2,3">Compute GDP per capita</subtask>
</plan>
<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="direct_answer">Country whose capital is Lima</route>
<route round="1" subtask="2" model="claude-haiku-4-5-20251001" skill="web_search">Peru 2023 nominal GDP</route>
<route round="1" subtask="3" model="claude-haiku-4-5-20251001" skill="web_search">Peru 2023 population</route>
<route round="1" subtask="4" model="gpt-5.3-codex" skill="execute_python">Compute GDP per capita</route>
[/ASSISTANT]
[TOOL]
<obs subtask="1">Peru.</obs>
<obs subtask="2">Peru 2023 nominal GDP: $267.6 billion USD.</obs>
<obs subtask="3">Peru 2023 population: 34.35 million.</obs>
<obs subtask="4">267600000000 / 34350000 = 7790.39 USD.</obs>
[/TOOL]
[ASSISTANT]
<verify round="1" status="pass">All four subtasks succeeded; values are consistent.</verify>
<final_answer>Approximately $7,790 USD per capita (Peru, 2023).</final_answer>
[/ASSISTANT]

## Exemplar 3: observation-driven continuation
(Round 1 lists the part of the DAG we can express up front. Round 2's subtasks become
expressible only AFTER seeing round 1's obs.)

[ASSISTANT]
<plan round="1">
  <subtask id="1" depends_on="">List the Python files in the repository's auth/ directory</subtask>
</plan>
<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="execute_python">ls auth/*.py</route>
[/ASSISTANT]
[TOOL]
<obs subtask="1">login.py, session.py, oauth.py</obs>
[/TOOL]
[ASSISTANT]
<verify round="1" status="repair_needed" target="1">Round 1 obs reveals the candidate files; round 2 can now plan reads against the specific files that exist.</verify>
<plan round="2">
  <subtask id="2" depends_on="">Read auth/login.py and locate the password hashing call</subtask>
  <subtask id="3" depends_on="">Read auth/session.py and locate the session token construction</subtask>
  <subtask id="4" depends_on="2,3">Identify whether the password hash is reused as the session token</subtask>
</plan>
<route round="2" subtask="2" model="claude-opus-4-6" skill="extract_field">extract the hashing call site from auth/login.py</route>
<route round="2" subtask="3" model="claude-opus-4-6" skill="extract_field">extract the session token construction from auth/session.py</route>
<route round="2" subtask="4" model="gpt-5.4" skill="direct_answer">compare the two extracted snippets and report whether they share state</route>
[/ASSISTANT]
[TOOL]
<obs subtask="2">login.py line 42: session_token = sha256(password)</obs>
<obs subtask="3">session.py line 17: session_token is read from cookie unchanged</obs>
<obs subtask="4">Yes — the session token is the SHA256 of the raw password, which is then stored in the cookie. This is a reuse anti-pattern.</obs>
[/TOOL]
[ASSISTANT]
<verify round="2" status="pass">All three round-2 subtasks succeeded; the comparison answers the question.</verify>
<final_answer>Yes — auth/login.py uses sha256(password) as the session token, and auth/session.py reads that same value from the cookie. The password hash is reused as the session token, which is a known anti-pattern.</final_answer>
[/ASSISTANT]

# PRIMARY DECISION: how to decompose
The MOST important decision in this trajectory is the structure of <plan>: which
subtasks to create, which dependencies to declare, and which subtasks to omit
entirely (lazy mode). Model and skill selection is a SECONDARY decision. A great
trajectory has a great plan; an okay plan with a great model is worse than a great
plan with an okay model. When you write the plan, ask yourself: "is this the right
way to split THIS question, or am I just listing surface keywords?"

# When to use multiple plan rounds
A second <plan round="2"> is for OBSERVATION-DRIVEN CONTINUATION, not failure
recovery. Use it when round 1's subtasks could only safely express the
observation-independent part of the DAG, and round 2's subtasks become expressible
only AFTER the round 1 obs are available. Examples:
- Round 1: list the files in the repo. Round 2: read the specific file the listing
  revealed as relevant.
- Round 1: identify the entity. Round 2: query that entity's attributes.
- Round 1: run the test suite. Round 2: fix the specific failures the test output revealed.
This is fundamentally different from "round 1 failed, retry with a stronger model".
The verifier outputs status="repair_needed" with target=<list of subtask ids whose
obs unblocked the next round>; the reason should explicitly say "this round
unblocked the following subtasks" rather than "round 1 failed".

# Cost-aware behavior (SECONDARY)
- Pick the WEAKEST model that can plausibly solve each subtask.
- Use direct_solve when no tool is needed.
- Reserve opus / gpt-5.4 for the hardest subtasks.
- Use gpt-5.3-codex (or qwen3-coder-plus) for code subtasks.

# Diversity nudge (soft)
When several models could handle the same subtask, vary across families:
- gpt-5.4 / gpt-5.3-codex: strong on code and reasoning
- kimi-k2.5: long-context at lower cost
- gemini-3.1-pro-preview / gemini-2.5-flash: flash is very cheap for simple tasks
- qwen3.6-plus: Alibaba frontier, strong on coding benchmarks

Skills DELIBERATELY OVERLAP — choose based on cost and context, not keywords:
- symbolic_math vs execute_python: both do math, symbolic_math is much cheaper
- web_search vs database_query: both find info, different sources
- read_document vs read_code: both read, different content types
- execute_python vs call_api: both invoke external systems, different interfaces
- reason vs direct_answer: both use model only, reason is deeper and costlier
Pick the CHEAPEST skill that can plausibly handle each subtask.

You will receive ONE question + the correct answer + a behavioral hint. Output ONE trajectory in the [ASSISTANT]/[TOOL] format above.
"""


def build_user_prompt(question: str, gold: str, behavior: str, evidence: str | None = None) -> str:
    hint_map = {
        BEHAVIOR_LAZY: (
            "BEHAVIORAL HINT: This question is well within parametric knowledge. Use LAZY MODE: "
            "output <final_answer> directly with NO <plan> and NO <route>."
        ),
        BEHAVIOR_ONESHOT: (
            "BEHAVIORAL HINT: Generate a clean ONE-SHOT trajectory: plan once, route in parallel where "
            "possible, verify pass, final answer. The plan should fully describe the DAG; do NOT split "
            "the DAG across rounds unless an observation is genuinely required to express later subtasks."
        ),
        BEHAVIOR_CONTINUATION: (
            "BEHAVIORAL HINT: Generate an OBSERVATION-DRIVEN CONTINUATION trajectory. Round 1's plan "
            "emits ONLY the part of the DAG that can be expressed without seeing any obs (often a single "
            "exploratory subtask: list files, identify entity, run test suite, search broadly). Round 1 "
            "verify is status=\"repair_needed\" with target listing the round-1 subtasks whose obs unblock "
            "the next phase, and a reason that explicitly says the next round became expressible. Round 2 "
            "emits the rest of the DAG, conditioned on what was observed. This is NOT failure recovery; "
            "round 1 succeeded — it just couldn't express round 2 in advance."
        ),
        BEHAVIOR_DECOMP_REPAIR: (
            "BEHAVIORAL HINT: Generate a trajectory where round 1's plan is STRUCTURALLY WRONG (wrong "
            "subtask boundaries, missing a key step, wrong dependency direction, or wrong granularity). "
            "The verifier identifies this as a DECOMPOSITION error (not a tool/model failure), and "
            "round 2's <plan> emits a substantially different DAG that fixes the structure. The repair "
            "is about HOW to split, not which model to call. Use this branch sparingly and only when the "
            "question genuinely lends itself to a structural mistake."
        ),
    }
    hint = hint_map.get(behavior, hint_map[BEHAVIOR_ONESHOT])

    # Evidence injection: if real evidence is available from the dataset,
    # include it so the teacher writes obs BASED ON real evidence instead
    # of hallucinating. This ensures trajectory-internal consistency while
    # grounding obs in real data.
    evidence_block = ""
    if evidence and len(evidence.strip()) > 10:
        truncated = evidence[:3000] if len(evidence) > 3000 else evidence
        evidence_block = f"""

REAL EVIDENCE (from the dataset — use this as the basis for ALL <obs> content):
{truncated}

IMPORTANT: Your <obs> tags MUST reflect this real evidence, not made-up information.
Paraphrase or excerpt from the evidence above. Do NOT invent facts not in the evidence."""

    return f"""Question: {question}

Correct answer (for your reference; arrive at this through proper decomposition): {gold}
{evidence_block}

{hint}

Output the trajectory now."""


# ---------------------------------------------------------------------------
# Output parsing: teacher text -> messages list
# ---------------------------------------------------------------------------


TURN_RE = re.compile(r"\[(ASSISTANT|TOOL)\](.*?)\[/\1\]", re.DOTALL)


def parse_teacher_output(text: str, system_prompt: str, user_prompt: str) -> list[dict] | None:
    """Parse the teacher text into a ChatML messages list.

    Returns None if no [ASSISTANT] / [TOOL] markers were found.
    """
    matches = list(TURN_RE.finditer(text))
    if not matches:
        return None
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for m in matches:
        role_marker = m.group(1)
        content = m.group(2).strip()
        role = "assistant" if role_marker == "ASSISTANT" else "tool"
        messages.append({"role": role, "content": content})
    return messages


# ---------------------------------------------------------------------------
# Teacher API call
# ---------------------------------------------------------------------------


@dataclass
class TeacherCall:
    text: str
    input_tokens: int
    output_tokens: int
    elapsed_sec: float
    error: str | None = None


def call_teacher(
    client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    extra_user: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> TeacherCall:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt + ("\n\n" + extra_user if extra_user else "")},
    ]
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        in_toks = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_toks = getattr(usage, "completion_tokens", 0) if usage else 0
        return TeacherCall(text=text, input_tokens=in_toks, output_tokens=out_toks, elapsed_sec=time.monotonic() - t0)
    except Exception as e:  # noqa: BLE001
        return TeacherCall(
            text="",
            input_tokens=0,
            output_tokens=0,
            elapsed_sec=time.monotonic() - t0,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


# ---------------------------------------------------------------------------
# Distillation loop
# ---------------------------------------------------------------------------


@dataclass
class DistilledSample:
    id: str
    source: str
    domain: str
    behavior: str
    teacher: str
    messages: list[dict]
    gold: str
    valid: bool
    n_attempts: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stats: dict
    errors: list[str] = field(default_factory=list)


def estimate_cost(model: str, in_toks: int, out_toks: int) -> float:
    rates = COST_TABLE.get(model, {"in": 0.0, "out": 0.0})
    return (in_toks / 1_000_000) * rates["in"] + (out_toks / 1_000_000) * rates["out"]


def distill_one(
    client,
    pools: dict,
    source: str,
    domain: str,
    teacher: str,
    question: str,
    gold: str,
    behavior: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    evidence: str | None = None,
) -> DistilledSample:
    system_prompt = build_system_prompt(pools)
    user_prompt = build_user_prompt(question, gold, behavior, evidence=evidence)

    sample_id = f"{source}_{abs(hash(question)) % 10**8:08d}"
    total_in = 0
    total_out = 0
    errors: list[str] = []
    last_messages: list[dict] = []
    extra_user = None
    for attempt in range(1, max_retries + 1):
        call = call_teacher(client, teacher, system_prompt, user_prompt, extra_user)
        total_in += call.input_tokens
        total_out += call.output_tokens
        if call.error:
            errors.append(f"attempt {attempt}: API error: {call.error}")
            time.sleep(2)
            continue
        parsed = parse_teacher_output(call.text, system_prompt, user_prompt)
        if parsed is None:
            errors.append(f"attempt {attempt}: no [ASSISTANT]/[TOOL] markers in teacher output")
            extra_user = (
                "Your previous output had no [ASSISTANT]/[TOOL] markers. Re-emit the trajectory using the "
                "exact [ASSISTANT]...[/ASSISTANT] [TOOL]...[/TOOL] format from the system prompt."
            )
            continue
        result = validate_messages(parsed, pools=pools)
        last_messages = parsed
        if result.valid:
            return DistilledSample(
                id=sample_id,
                source=source,
                domain=domain,
                behavior=behavior,
                teacher=teacher,
                messages=parsed,
                gold=gold,
                valid=True,
                n_attempts=attempt,
                input_tokens=total_in,
                output_tokens=total_out,
                cost_usd=estimate_cost(teacher, total_in, total_out),
                stats=result.stats,
                errors=errors,
            )
        # Validation failed: prepare retry prompt with the error list.
        err_summary = "; ".join(f"[{e.code}] {e.message[:120]}" for e in result.errors[:5])
        errors.append(f"attempt {attempt}: VALIDATOR errors: {err_summary}")
        extra_user = (
            "Your previous output failed schema validation. The validator reported these errors:\n"
            f"{err_summary}\n\n"
            "Re-emit the entire trajectory, fixing every error. Pay special attention to: "
            "depends_on always present, route.round matching plan.round, verify status enum, "
            "exactly one final_answer in the LAST assistant turn, and the per-model allowed_skills whitelist."
        )

    # All retries exhausted.
    return DistilledSample(
        id=sample_id,
        source=source,
        domain=domain,
        behavior=behavior,
        teacher=teacher,
        messages=last_messages,
        gold=gold,
        valid=False,
        n_attempts=max_retries,
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=estimate_cost(teacher, total_in, total_out),
        stats={},
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Query sampling from a recipe entry
# ---------------------------------------------------------------------------


def fetch_query_pool(entry: dict, n: int, max_retries: int = 2) -> list[tuple[str, str, str | None]]:
    """Stream from HF and return up to `n` (question, gold_answer) pairs.

    Robust to HF Hub SSL flakes: if the iteration aborts mid-way, retry up to
    `max_retries` times. If still empty after retries, return whatever was
    collected so far (possibly empty).
    """
    from datasets import load_dataset  # noqa: WPS433

    hf_path = entry["hf_path"]
    hf_subset = entry.get("hf_subset")
    split = entry["split"]

    out: list[tuple[str, str, str | None]] = []
    for attempt in range(max_retries + 1):
        try:
            if hf_subset:
                ds = load_dataset(hf_path, hf_subset, split=split, streaming=True)
            else:
                ds = load_dataset(hf_path, split=split, streaming=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ! load_dataset failed (attempt {attempt + 1}): {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2)
                continue
            return out
        try:
            for row in ds:
                q, g = _row_to_qg(entry["name"], row)
                evidence = _extract_evidence(entry["name"], row)
                if q and g:
                    out.append((q, g, evidence))
                if len(out) >= n:
                    return out
        except Exception as e:  # noqa: BLE001
            print(f"  ! iteration aborted (attempt {attempt + 1}): {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            if len(out) >= max(1, n // 3):
                # We got at least some samples; good enough.
                return out
            if attempt < max_retries:
                time.sleep(2)
                continue
        # If we reach here, the iteration ended naturally without enough samples.
        return out
    return out


def _row_to_qg(dataset_name: str, row: dict) -> tuple[str, str]:
    """Per-dataset (question, gold) extractor. Best-effort, easy to extend."""
    name = dataset_name.lower()

    def first_str(*keys):
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, list) and v:
                vv = v[0]
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
            if isinstance(v, dict):
                # nested answer fields
                for k2 in ("text", "answer", "value"):
                    vv = v.get(k2)
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
                    if isinstance(vv, list) and vv and isinstance(vv[0], str):
                        return vv[0].strip()
        return ""

    if "hotpotqa" in name:
        return first_str("question"), first_str("answer")
    if "2wiki" in name:
        return first_str("question"), first_str("answer")
    if "musique" in name:
        return first_str("question"), first_str("answer")
    if "strategyqa" in name:
        return first_str("question"), str(row.get("answer", ""))
    if "nq_open" in name:
        return first_str("question"), first_str("answer")
    if "triviaqa" in name:
        return first_str("question"), first_str("answer")
    if "webquestions" in name:
        return first_str("question"), first_str("answers")
    if "gsm8k" in name:
        return first_str("question"), first_str("answer")
    if "hendrycks_math" in name:
        return first_str("problem"), first_str("solution")
    if "theoremqa" in name:
        return first_str("Question"), str(row.get("Answer", ""))
    if "aqua" in name:
        return first_str("question"), first_str("correct")
    if "codeforces" in name:
        return first_str("description", "title", "name"), first_str("editorial", "tags")
    if "codecontests" in name:
        return first_str("description"), str(row.get("solutions", {}).get("solution", [""])[0] if isinstance(row.get("solutions"), dict) else "")
    if "sciq" in name:
        return first_str("question"), first_str("correct_answer")
    if "arc" in name:
        return first_str("question"), first_str("answerKey")
    if "openbookqa" in name:
        q = first_str("question_stem")
        a = first_str("answerKey")
        return q, a
    if "mmlu" in name:
        q = first_str("question")
        choices = row.get("choices", [])
        a_idx = row.get("answer", 0)
        a = choices[a_idx] if isinstance(choices, list) and 0 <= int(a_idx) < len(choices) else str(a_idx)
        return q, str(a)
    if "commonsenseqa" in name:
        return first_str("question"), first_str("answerKey")
    if "piqa" in name:
        q = first_str("goal")
        label = row.get("label", 0)
        sol = row.get(f"sol{int(label)+1}", "")
        return q, str(sol)
    if "social_iqa" in name or "siqa" in name:
        q = first_str("context") + " " + first_str("question")
        label = row.get("label", "1")
        a = row.get(f"answer{['A','B','C'][int(label)-1]}", "")
        return q.strip(), str(a)
    if "winogrande" in name:
        q = first_str("sentence")
        opts = [row.get("option1", ""), row.get("option2", "")]
        ans_idx = int(row.get("answer", "1")) - 1
        return q, opts[ans_idx] if 0 <= ans_idx < 2 else ""
    if "logiqa" in name:
        # datatune/LogiQA2.0 stores a JSON string in `text` containing nested
        # {id, answer, text, options}.
        raw = row.get("text", "")
        if isinstance(raw, str):
            try:
                import json as _json  # noqa: WPS433
                obj = _json.loads(raw)
                inner_text = obj.get("text", "")
                opts = obj.get("options", [])
                ans_idx = obj.get("answer", -1)
                gold = ""
                if isinstance(opts, list) and 0 <= int(ans_idx) < len(opts):
                    gold = str(opts[int(ans_idx)])
                else:
                    gold = str(ans_idx)
                return inner_text.strip(), gold
            except Exception:  # noqa: BLE001
                return raw[:1500], ""
        return "", ""
    if "folio" in name:
        return first_str("conclusion"), first_str("conclusion-FOL")
    if "bbh" in name:
        return first_str("input"), first_str("target")
    if "quality" in name:
        # emozilla/quality: long article + multiple-choice question.
        article = (row.get("article") or "")[:1500]
        q = row.get("question", "")
        opts = row.get("options", [])
        ans_idx = row.get("answer", -1)
        gold = ""
        if isinstance(opts, list) and 0 <= int(ans_idx) < len(opts):
            gold = str(opts[int(ans_idx)])
        full_q = f"{article}\n\nQuestion: {q}".strip()
        return full_q, gold
    if "legalbench" in name:
        return first_str("text", "question"), first_str("answer")
    if "finqa" in name or "flare" in name:
        return first_str("query", "question"), first_str("answer")
    if "toolace" in name:
        # Team-ACE/ToolACE stores multi-turn data in `conversations`: list of
        # {from, value} dicts. We pull the first user turn as the question and
        # the first assistant turn as the gold (a function-call string).
        convs = row.get("conversations") or []
        if isinstance(convs, list):
            user_turn = next((c.get("value", "") for c in convs if c.get("from") == "user"), "")
            ast_turn = next((c.get("value", "") for c in convs if c.get("from") == "assistant"), "")
            return str(user_turn).strip(), str(ast_turn).strip()
        return "", ""
    return first_str("question", "query", "input"), first_str("answer", "target", "response")


def _extract_evidence(dataset_name: str, row: dict) -> str | None:
    """Extract real evidence/context from dataset row (if available).

    Returns the evidence text, or None if the dataset doesn't provide it.
    This evidence will be injected into the distillation prompt so the
    teacher writes obs based on real data instead of hallucinating.
    """
    name = dataset_name.lower()

    def _join(items, max_items=5, max_chars=3000):
        parts = []
        for item in items[:max_items]:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", item.get("paragraph_text", str(item)))))
            elif isinstance(item, list):
                parts.append(" ".join(str(x) for x in item[:5]))
        return "\n".join(parts)[:max_chars]

    if "hotpotqa" in name:
        ctx = row.get("context", {})
        titles = ctx.get("title", []) if isinstance(ctx, dict) else []
        sents = ctx.get("sentences", []) if isinstance(ctx, dict) else []
        parts = []
        for t, s in zip(titles, sents):
            text = " ".join(s) if isinstance(s, list) else str(s)
            if text.strip():
                parts.append(f"[{t}] {text}")
        return "\n".join(parts[:5])[:3000] if parts else None
    if "2wiki" in name:
        ev = row.get("evidences", [])
        if ev:
            return _join(ev)
        return None
    if "musique" in name:
        paras = row.get("paragraphs", [])
        if paras:
            return _join(paras)
        return None
    if "strategyqa" in name:
        ev = row.get("evidence", [])
        if ev:
            parts = []
            for e in ev[:5]:
                if isinstance(e, list):
                    parts.extend(str(x) for x in e[:3])
                elif isinstance(e, str):
                    parts.append(e)
            return "\n".join(parts)[:3000] if parts else None
        return None
    if "gsm8k" in name:
        return row.get("answer")  # Full step-by-step solution
    if "hendrycks_math" in name or "math" in name:
        return row.get("solution")
    if "aqua" in name:
        return row.get("rationale")
    if "codecontests" in name:
        sols = row.get("solutions", {})
        if isinstance(sols, dict):
            sol_list = sols.get("solution", [])
            if sol_list:
                return str(sol_list[0])[:3000]
        return None
    if "codeforces" in name:
        return row.get("editorial", None)
    if "nq_open" in name or ("nq" in name and "wiki" not in name):
        # natural_questions full version has document field
        doc = row.get("document", {})
        if isinstance(doc, dict):
            text = doc.get("text", "") or doc.get("html", "")
            if text:
                return str(text)[:3000]
        # nq_open has no context — return None
        return None
    if "triviaqa" in name:
        # rc/unfiltered splits have search_results and entity_pages
        sr = row.get("search_results", {})
        if isinstance(sr, dict):
            contexts = sr.get("search_context", [])
            if contexts and any(contexts):
                return "\n".join(str(c)[:600] for c in contexts if c)[:3000]
        ep = row.get("entity_pages", {})
        if isinstance(ep, dict):
            wiki = ep.get("wiki_context", [])
            if wiki and any(wiki):
                return "\n".join(str(c)[:600] for c in wiki if c)[:3000]
        return None
    if "sciq" in name:
        return row.get("support")
    if "openbookqa" in name:
        return row.get("fact1")
    if "quality" in name:
        article = row.get("article", "")
        return article[:3000] if article else None
    if "logiqa" in name:
        raw = row.get("text", "")
        if isinstance(raw, str) and raw.startswith("{"):
            try:
                import json as _json
                obj = _json.loads(raw)
                return obj.get("text", "")[:3000]
            except Exception:
                pass
        return raw[:3000] if raw else None
    if "folio" in name:
        return row.get("premises")
    if "bbh" in name:
        return row.get("input")
    if "legalbench" in name:
        return row.get("text", "")[:3000] or None
    if "finqa" in name or "flare" in name:
        return row.get("text", row.get("query", ""))[:3000] or None
    if "toolace" in name:
        convs = row.get("conversations", [])
        tool_resps = [c.get("value", "") for c in convs if c.get("from") == "tool"]
        return "\n".join(tool_resps[:3])[:3000] if tool_resps else None
    return None


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def stratified_sampling(recipe: dict, target: dict[str, int]) -> list[tuple[dict, int]]:
    """Pick datasets such that the per-domain count matches `target`.

    Returns a list of (recipe_entry, n_to_take). Within a domain we round-robin
    across the available datasets to maximize diversity.
    """
    by_domain: dict[str, list[dict]] = {}
    for e in recipe["datasets"]:
        by_domain.setdefault(e["domain"], []).append(e)
    out: list[tuple[dict, int]] = []
    for domain, n in target.items():
        entries = by_domain.get(domain, [])
        if not entries:
            continue
        # Round-robin assign
        per = [0] * len(entries)
        for i in range(n):
            per[i % len(entries)] += 1
        for entry, k in zip(entries, per):
            if k > 0:
                out.append((entry, k))
    return out


def write_summary(out_path: str, samples: list[DistilledSample]) -> None:
    n = len(samples)
    n_valid = sum(1 for s in samples if s.valid)
    cost = sum(s.cost_usd for s in samples)
    by_domain: dict[str, dict[str, int]] = {}
    for s in samples:
        d = by_domain.setdefault(s.domain, {"total": 0, "valid": 0})
        d["total"] += 1
        if s.valid:
            d["valid"] += 1
    by_behavior: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_skill: dict[str, int] = {}
    for s in samples:
        if not s.valid:
            continue
        by_behavior[s.behavior] = by_behavior.get(s.behavior, 0) + 1
        for m in s.stats.get("models_used", []):
            by_model[m] = by_model.get(m, 0) + 1
        for sk in s.stats.get("skills_used", []):
            by_skill[sk] = by_skill.get(sk, 0) + 1
    n_lazy = sum(1 for s in samples if s.valid and s.stats.get("is_lazy"))
    n_repair = sum(1 for s in samples if s.valid and s.stats.get("n_repair_rounds", 0) > 0)
    summary = {
        "n_total": n,
        "n_valid": n_valid,
        "valid_rate": (n_valid / n) if n else 0.0,
        "lazy_fraction": (n_lazy / n_valid) if n_valid else 0.0,
        "repair_fraction": (n_repair / n_valid) if n_valid else 0.0,
        "total_cost_usd": round(cost, 4),
        "avg_attempts": round(sum(s.n_attempts for s in samples) / max(n, 1), 2),
        "by_domain": by_domain,
        "by_behavior": by_behavior,
        "by_model": by_model,
        "by_skill": by_skill,
    }
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", default=RECIPE_PATH)
    parser.add_argument("--pools", default=POOLS_PATH)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--out-name", default="dryrun", help="prefix for {out}.jsonl + {out}_summary.json")
    parser.add_argument("--smoke", action="store_true", help="distill one sample from one dataset")
    parser.add_argument("--dryrun", action="store_true", help="distill 30 stratified samples (Phase A5)")
    parser.add_argument("--pilot", action="store_true", help="distill 400 stratified samples (Phase B1)")
    parser.add_argument("--full", action="store_true", help="Phase C: full recipe (41.5k), uses async concurrency")
    parser.add_argument("--concurrency", type=int, default=20, help="max parallel API calls for --full mode")
    parser.add_argument("--only", default=None, help="distill from a single dataset by name")
    parser.add_argument("--n", type=int, default=1, help="number of samples per --only dataset")
    parser.add_argument("--api-key", default=None, help="overrides XIAOJING_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    api_key = args.api_key or os.environ.get("XIAOJING_API_KEY")
    if not api_key:
        print("ERROR: set XIAOJING_API_KEY env var or pass --api-key", file=sys.stderr)
        return 2

    from openai import OpenAI  # noqa: WPS433

    client = OpenAI(api_key=api_key, base_url=args.base_url)
    pools = load_pools(args.pools)

    import yaml  # noqa: WPS433

    with open(args.recipe) as f:
        recipe = yaml.safe_load(f)

    # Build the work plan: list of (entry, n_to_take, behavior).
    work: list[tuple[dict, int]] = []
    if args.smoke:
        work = [(recipe["datasets"][0], 1)]
    elif args.dryrun:
        work = stratified_sampling(recipe, A5_STRATIFICATION)
        # Plus free slots from the largest available domain.
        if A5_FREE_SLOTS > 0:
            largest_domain = max(A5_STRATIFICATION, key=A5_STRATIFICATION.get)
            extras = [(e, 1) for e in recipe["datasets"] if e["domain"] == largest_domain][:A5_FREE_SLOTS]
            work.extend(extras)
    elif args.pilot:
        work = stratified_sampling(recipe, B1_STRATIFICATION)
    elif args.full:
        # Phase C: use every dataset's full n_samples from recipe
        work = [(e, e["n_samples"]) for e in recipe["datasets"]]
    elif args.only:
        entries = [e for e in recipe["datasets"] if e["name"] == args.only]
        if not entries:
            print(f"no dataset named {args.only}", file=sys.stderr)
            return 2
        work = [(entries[0], args.n)]
    else:
        print("specify one of --smoke / --dryrun / --pilot / --full / --only", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    out_jsonl = os.path.join(args.out_dir, f"{args.out_name}.jsonl")
    failed_jsonl = os.path.join(args.out_dir, f"{args.out_name}_failed.jsonl")
    summary_json = os.path.join(args.out_dir, f"{args.out_name}_summary.json")

    total_target = sum(n for _, n in work)
    print(f"distilling {total_target} samples across {len(work)} dataset entries")
    print(f"output: {out_jsonl}")
    if args.full:
        print(f"MODE: async full (concurrency={args.concurrency})")

    # Behavior cycle
    behaviors_cycle = (
        [BEHAVIOR_ONESHOT] * 10
        + [BEHAVIOR_CONTINUATION] * 6
        + [BEHAVIOR_LAZY] * 3
        + [BEHAVIOR_DECOMP_REPAIR] * 1
    )
    random.shuffle(behaviors_cycle)

    # Resume support
    seen_ids: set[str] = set()
    if os.path.exists(out_jsonl):
        with open(out_jsonl) as f:
            for line in f:
                try:
                    seen_ids.add(json.loads(line)["id"])
                except Exception:
                    pass
    if seen_ids:
        print(f"resuming: {len(seen_ids)} samples already done")

    # ---------------------------------------------------------------
    # Build flat task list: [(source, domain, teacher, question, gold, behavior), ...]
    # ---------------------------------------------------------------
    tasks: list[tuple[str, str, str, str, str, str, str | None]] = []
    bcursor = 0
    for entry, k in work:
        teacher = entry.get("distill_model", "claude-sonnet-4-6")
        print(f"  loading [{entry['name']}] n={k} ...", end=" ", flush=True)
        try:
            pool = fetch_query_pool(entry, k * 3)
        except Exception as e:
            print(f"FAIL: {type(e).__name__}")
            continue
        if not pool:
            print("empty")
            continue
        random.shuffle(pool)
        added = 0
        for q, gold, evidence in pool:
            if added >= k:
                break
            sample_id = f"{entry['name']}_{abs(hash(q)) % 10**8:08d}"
            if sample_id in seen_ids:
                added += 1
                continue
            behavior = behaviors_cycle[bcursor % len(behaviors_cycle)]
            bcursor += 1
            tasks.append((entry["name"], entry["domain"], teacher, q, gold, behavior, evidence))
            added += 1
        n_ev = sum(1 for _, _, _, _, _, _, ev in tasks[-added:] if ev)
        print(f"ok ({added} tasks, {n_ev} with evidence)")

    print(f"\ntotal tasks to distill: {len(tasks)}")

    # ---------------------------------------------------------------
    # Async runner (for --full) or sync runner (for others)
    # ---------------------------------------------------------------
    if args.full and len(tasks) > 50:
        import asyncio
        return asyncio.run(_async_distill(
            tasks=tasks,
            pools=pools,
            api_key=api_key,
            base_url=args.base_url,
            concurrency=args.concurrency,
            out_jsonl=out_jsonl,
            failed_jsonl=failed_jsonl,
            summary_json=summary_json,
        ))
    else:
        return _sync_distill(
            tasks=tasks,
            pools=pools,
            client=client,
            out_jsonl=out_jsonl,
            failed_jsonl=failed_jsonl,
            summary_json=summary_json,
        )


def _sync_distill(tasks, pools, client, out_jsonl, failed_jsonl, summary_json) -> int:
    samples: list[DistilledSample] = []
    for i, (source, domain, teacher, q, gold, behavior, evidence) in enumerate(tasks, 1):
        sample = distill_one(client, pools, source, domain, teacher, q, gold, behavior, evidence=evidence)
        samples.append(sample)
        target_file = out_jsonl if sample.valid else failed_jsonl
        with open(target_file, "a") as f:
            f.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
        glyph = "✅" if sample.valid else "❌"
        print(f"[{i}/{len(tasks)}] {glyph} {sample.id}  {behavior}  att={sample.n_attempts}  ${sample.cost_usd:.4f}")
    write_summary(summary_json, samples)
    return 0 if all(s.valid for s in samples) else 1


async def _async_distill(tasks, pools, api_key, base_url, concurrency, out_jsonl, failed_jsonl, summary_json) -> int:
    from openai import AsyncOpenAI

    aclient = AsyncOpenAI(api_key=api_key, base_url=base_url)
    sem = __import__("asyncio").Semaphore(concurrency)
    file_lock = __import__("asyncio").Lock()
    samples: list[DistilledSample] = []
    done_count = [0]
    total = len(tasks)
    t0_global = time.monotonic()

    async def process_one(source, domain, teacher, q, gold, behavior, evidence):
        async with sem:
            # Reuse the sync distill_one but wrap the blocking call
            loop = __import__("asyncio").get_event_loop()
            # Create a per-task sync client (thread-safe)
            from openai import OpenAI
            sync_client = OpenAI(api_key=api_key, base_url=base_url)
            sample = await loop.run_in_executor(
                None,
                lambda: distill_one(sync_client, pools, source, domain, teacher, q, gold, behavior, evidence=evidence),
            )
        async with file_lock:
            samples.append(sample)
            target_file = out_jsonl if sample.valid else failed_jsonl
            with open(target_file, "a") as f:
                f.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
            done_count[0] += 1
            n = done_count[0]
            elapsed = time.monotonic() - t0_global
            rate = n / elapsed if elapsed > 0 else 0
            eta_min = (total - n) / rate / 60 if rate > 0 else 0
            glyph = "✅" if sample.valid else "❌"
            if n % 50 == 0 or not sample.valid:
                valid_so_far = sum(1 for s in samples if s.valid)
                cost_so_far = sum(s.cost_usd for s in samples)
                print(
                    f"[{n}/{total}] {glyph} {sample.id}  {behavior}  "
                    f"valid={valid_so_far}/{n} ({100*valid_so_far/n:.1f}%)  "
                    f"${cost_so_far:.2f}  {rate:.1f}/s  ETA {eta_min:.0f}min"
                )

    print(f"launching {total} tasks with concurrency={concurrency}")
    import asyncio
    await asyncio.gather(*(
        process_one(src, dom, teach, q, g, beh, ev)
        for src, dom, teach, q, g, beh, ev in tasks
    ))

    elapsed_total = time.monotonic() - t0_global
    print(f"\ndone: {len(samples)} samples in {elapsed_total/60:.1f} min")
    write_summary(summary_json, samples)
    return 0 if all(s.valid for s in samples) else 1


if __name__ == "__main__":
    sys.exit(main())
