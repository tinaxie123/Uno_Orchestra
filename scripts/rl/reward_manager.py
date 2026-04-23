"""
SkillRouter reward manager for GRPO training.

Reward: R = (1 - α) * R_correctness + α * R_cost
  - R_correctness: per-source verifier (math/qa/code/toolace)
  - R_cost: sqrt + percentile-normalized API cost (1=cheapest, 0=most expensive)
  - Format invalid → R = -1 (hard penalty)
  - α: adaptive schedule (ramps from alpha_init to alpha_final)

Compatible with Router-R1's verl RewardManager interface.
"""
from __future__ import annotations

import json
import math
import re
import string
from collections import Counter, deque

import numpy as np
import torch

# ── Cost computation from trajectory text ─────────────────────
#
# In GRPO generate-then-score mode, there's no real API call.
# We estimate cost from the router's chosen models (extracted from text)
# using pools.yaml pricing.
#
# cost_reward ∈ [0, 1]:
#   1.0 = cheapest possible (direct answer, no delegation)
#   0.0 = most expensive (multiple frontier model calls)
#
# Formula:
#   estimated_cost = Σ (model_output_price × assumed_tokens) for each route
#   cost_reward = 1 - clamp(estimated_cost / budget_cap, 0, 1)

# Model prices ($/1M tokens) — official pricing as of April 2026
MODEL_INPUT_PRICES = {
    "gemini-2.5-flash-lite": 0.10,
    "gemini-2.5-flash": 0.15,
    "kimi-k2.5": 0.35,
    "gemini-3-flash-preview": 0.50,
    "gemini-3.1-pro-preview": 2.50,
    "gpt-5.3-codex": 1.75,
    "gpt-5.4": 2.50,
    "claude-sonnet-4-6": 3.00,
    "claude-opus-4-6": 15.00,
}
MODEL_OUTPUT_PRICES = {
    "gemini-2.5-flash-lite": 0.40,
    "gemini-2.5-flash": 0.60,
    "kimi-k2.5": 2.50,
    "gemini-3-flash-preview": 3.00,
    "gemini-3.1-pro-preview": 12.00,
    "gpt-5.3-codex": 14.00,
    "gpt-5.4": 15.00,
    "claude-sonnet-4-6": 15.00,
    "claude-opus-4-6": 75.00,
}

# Assumed tokens per sub-agent call
# Input: question + subtask instruction (~1024 tokens)
# Output: response (~256 tokens)
ASSUMED_INPUT_TOKENS = 1024
ASSUMED_OUTPUT_TOKENS = 256

# Budget cap: cost above this → cost_reward = 0
# Roughly 2 calls to a mid-tier model ($3/M)
#   2 × (0.50×1024 + 3.00×256) / 1M = $0.002 per call × 2 = $0.004
BUDGET_CAP_USD = 0.01  # $0.01 per task


def _extract_routes(completion: str) -> list[dict]:
    """Extract routing decisions from trajectory text.

    Returns list of {"model": str, "instruction": str} for each delegation.
    Combines model selection and instruction extraction from multiple patterns.
    """
    # Step 1: Extract all instructions (from plan_subtask JSON)
    instructions = []
    for m in re.finditer(r'"instruction"\s*:\s*"((?:[^"\\]|\\.)*)"', completion):
        instructions.append(m.group(1).replace('\\"', '"').replace('\\n', '\n'))

    # Step 2: Extract all model selections
    models = []

    # Pattern A: <search>ModelName:query</search> (Router-R1 style)
    for m in re.finditer(r'<search>\s*([^:<>\n]+?)\s*:\s*(.*?)</search>', completion, re.DOTALL):
        name = m.group(1).strip()
        query = m.group(2).strip()
        if name in MODEL_OUTPUT_PRICES:
            models.append(name)
            if not instructions:
                instructions.append(query)

    # Pattern B: [routed to ModelName / skill]
    for m in re.finditer(r'\[routed to\s+([^\]/]+?)(?:\s*/\s*[\w_]+)?\]', completion):
        name = m.group(1).strip()
        if name in MODEL_OUTPUT_PRICES:
            models.append(name)

    # Pattern C: JSON "model": "name"
    for m in re.finditer(r'"(?:model|routed_model)"\s*:\s*"([^"]+)"', completion):
        name = m.group(1).strip()
        if name in MODEL_OUTPUT_PRICES:
            models.append(name)

    if not models and not instructions:
        return []

    # Step 3: Pair models with instructions
    n = max(len(models), len(instructions), 1)
    routes = []
    for i in range(n):
        model = models[i] if i < len(models) else (models[0] if models else "")
        instr = instructions[i] if i < len(instructions) else ""
        routes.append({"model": model, "instruction": instr})

    return routes


def estimate_cost(completion: str, tokenizer=None) -> float:
    """Estimate API cost (USD) from trajectory text.

    If tokenizer is provided, uses exact token count for instructions.
    Otherwise uses char/4 heuristic.

    Cost per call = input_price × input_tokens + output_price × output_tokens
    """
    routes = _extract_routes(completion)
    if not routes:
        return 0.0  # Direct answer, no delegation cost

    total = 0.0
    for route in routes:
        model = route["model"]
        instr = route["instruction"]

        inp_price = MODEL_INPUT_PRICES.get(model, 0.50)
        out_price = MODEL_OUTPUT_PRICES.get(model, 3.00)

        # Input tokens: tokenize instruction if possible, else estimate
        if instr and tokenizer is not None:
            try:
                input_tokens = len(tokenizer.encode(instr, add_special_tokens=False))
            except Exception:
                input_tokens = max(len(instr) // 4, ASSUMED_INPUT_TOKENS)
        elif instr:
            input_tokens = max(len(instr) // 4, 64)  # ~4 chars per token
        else:
            input_tokens = ASSUMED_INPUT_TOKENS

        call_cost = (inp_price * input_tokens + out_price * ASSUMED_OUTPUT_TOKENS) / 1e6
        total += call_cost

    return total


def compute_cost_reward(completion: str, tokenizer=None) -> float:
    """Compute cost reward ∈ [0, 1] where 1 = cheapest.

    Formula: 1 - clamp(estimated_cost / budget_cap, 0, 1)
    """
    cost = estimate_cost(completion, tokenizer)
    if cost <= 0:
        return 1.0  # No delegation = free = max reward
    ratio = min(cost / BUDGET_CAP_USD, 1.0)
    return 1.0 - ratio


def normalize_cost(raw_cost: float) -> float:
    """Legacy interface — wraps compute_cost_reward for backward compat."""
    return compute_cost_reward(str(raw_cost))


# ── Format validation ────────────────────────────────────────

def format_reward(completion: str) -> float:
    """Check if completion follows valid SkillRouter format.

    Valid formats:
    1. Direct: finish(answer) or {"name":"finish","arguments":{"answer":"..."}}
    2. Delegate: plan_subtask(...) → observation → ... → finish(answer)

    Returns 0.0 if valid, -1.0 if invalid.
    """
    text = completion.strip()
    if not text:
        return -1.0

    # Must contain a finish/answer action
    has_finish = bool(re.search(
        r'"name"\s*:\s*"finish"'
        r'|<final_answer>'
        r'|\bfinish\s*\('
        r'|"action"\s*:\s*"finish"',
        text,
    ))

    if not has_finish:
        return -1.0

    return 0.0


def extract_answer(completion: str) -> str | None:
    """Extract the final answer from router output."""
    text = completion.strip()

    # Pattern 1: {"name": "finish", "arguments": {"answer": "..."}}
    m = re.search(
        r'"name"\s*:\s*"finish".*?"arguments"\s*:\s*\{.*?"answer"\s*:\s*"(.*?)"',
        text, re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    # Pattern 2: <final_answer>...</final_answer>
    m = re.search(r'<final_answer>(.*?)</final_answer>', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Pattern 3: <answer>...</answer> (Router-R1 style)
    matches = list(re.finditer(r'<answer>(.*?)</answer>', text, re.DOTALL))
    if matches:
        return matches[-1].group(1).strip()

    # Pattern 4: finish("answer") or finish(answer)
    m = re.search(r'\bfinish\(\s*["\']?(.*?)["\']?\s*\)', text)
    if m:
        return m.group(1).strip()

    return None


def route_count(completion: str) -> int:
    """Count number of routing/delegation actions."""
    count = 0
    count += len(re.findall(r'"name"\s*:\s*"plan_subtask"', completion))
    count += len(re.findall(r'<search>', completion))
    return count


# ── Per-source scoring ───────────────────────────────────────
# Import dedicated verifiers
import sys, os
_verifier_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'verifiers')
if _verifier_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_verifier_dir))

from math_sym_verifier import verify_math
from toolace_call_verifier import verify_toolace
from code_exec_verifier import verify_code_exec


def _normalize_text(s: str) -> str:
    """Normalize for QA comparison."""
    s = s.lower()
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    s = "".join(ch for ch in s if ch not in string.punctuation)
    return " ".join(s.split())


def _f1_score(pred: str, gold: str) -> float:
    pred_toks = _normalize_text(pred).split()
    gold_toks = _normalize_text(gold).split()
    if not pred_toks or not gold_toks:
        return 0.0
    common = Counter(pred_toks) & Counter(gold_toks)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    prec = num_same / len(pred_toks)
    rec = num_same / len(gold_toks)
    return 2 * prec * rec / (prec + rec)


def _em_score(pred: str, gold: str) -> float:
    return 1.0 if _normalize_text(pred) == _normalize_text(gold) else 0.0


# ── Source → scorer mapping ──────────────────────────────────
# Uses the dedicated verifiers for each source type.

SOURCE_TO_SCORER = {
    # QA sources → token-level F1 (standard SQuAD-style)
    "drop": _f1_score,
    "hotpotqa": _f1_score,
    "musique": _f1_score,
    "knowledge_retrieval": _f1_score,
    "knowledge_composition": _f1_score,
    # Math → symbolic equivalence (sympy) with numeric/string fallback
    "gsm8k": verify_math,
    "numinamath": verify_math,
    "atomic_reasoning": verify_math,
    "compositional_reasoning": verify_math,
    # Code → execution-based (subprocess + test case comparison)
    "taco": lambda pred, gold: verify_code_exec(pred, gold),
    # Tool → structured API call matching (function name + params)
    "toolace": verify_toolace,
    "tool_orchestration": _f1_score,
}


def compute_correctness(pred: str, gold: str, data_source: str,
                        question: str = "") -> float:
    """Compute correctness score for a prediction.

    Args:
        pred: Predicted answer text
        gold: Gold answer
        data_source: Source identifier (determines which verifier to use)
        question: Original question (needed for TACO test case extraction)
    """
    if data_source in ("taco",):
        return verify_code_exec(pred, gold, question)
    scorer = SOURCE_TO_SCORER.get(data_source, _f1_score)
    return scorer(pred, gold)


# ── RewardManager (verl-compatible) ──────────────────────────

class SkillRouterRewardManager:
    """Drop-in replacement for Router-R1's RewardManager.

    Compatible with verl's reward_fn interface:
        reward_fn(data: DataProto) -> (metric, cost, reward) tensors
    """

    def __init__(self, config, tokenizer, num_examine=0, format_score=0.,
                 state="train", reward_metric="f1", max_turns=4,
                 max_obs_length=512, cost_coe=0.1):
        self.config = config
        self.tokenizer = tokenizer
        self.num_examine = num_examine
        self.state = state
        self.reward_metric = reward_metric
        self.cost_coe = cost_coe
        self._already_printed: dict[str, int] = {}

    def __call__(self, data):
        reward_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)
        cost_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)
        metric_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)

        # For val mode
        metric_em_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)
        metric_f1_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)
        route_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)

        for i in range(len(data)):
            data_item = data[i]
            prompt_ids = data_item.batch['prompts']
            prompt_length = prompt_ids.shape[-1]
            valid_response_length = data_item.batch['attention_mask'][prompt_length:].sum()
            response_ids = data_item.batch['responses'][:valid_response_length]

            # Decode response
            completion = self.tokenizer.decode(response_ids)
            data_source = data_item.non_tensor_batch['data_source']

            # Extract gold
            reward_model_data = data_item.non_tensor_batch['reward_model']
            if isinstance(reward_model_data, str):
                reward_model_data = json.loads(reward_model_data)
            gold = reward_model_data.get('ground_truth', '')

            # 1. Format check
            fmt_score = format_reward(completion)

            # 2. Extract answer
            answer = extract_answer(completion)

            # 3. Cost reward (from trajectory text, tokenizer for exact count)
            cost_r = compute_cost_reward(completion, self.tokenizer)

            # 4. Route count
            n_routes = route_count(completion)

            # 5. Compute correctness
            # Get question for TACO code execution
            extra_info = data_item.non_tensor_batch.get('extra_info', '{}')
            if isinstance(extra_info, str):
                try:
                    extra_info = json.loads(extra_info)
                except Exception:
                    extra_info = {}
            question = extra_info.get('question', '')

            if answer is None:
                correctness = 0.0
                em_score = 0.0
                f1_score_val = 0.0
            else:
                correctness = compute_correctness(answer, gold, data_source, question)
                em_score = _em_score(answer, gold)
                f1_score_val = _f1_score(answer, gold)

            # 6. Combine reward:
            #   Format invalid → -1.0
            #   Answer wrong   →  0.0
            #   Answer correct → (1-α) × correctness + α × cost_reward
            if fmt_score < 0:
                reward = -1.0
            elif correctness == 0:
                reward = 0.0
            else:
                reward = (1.0 - self.cost_coe) * correctness + self.cost_coe * cost_r

            # Place reward at last valid token
            reward_tensor[i, valid_response_length - 1] = reward
            cost_tensor[i, valid_response_length - 1] = cost_r
            route_tensor[i, valid_response_length - 1] = n_routes

            if self.state == "train":
                metric_tensor[i, valid_response_length - 1] = correctness
            else:
                metric_em_tensor[i, valid_response_length - 1] = em_score
                metric_f1_tensor[i, valid_response_length - 1] = f1_score_val

            # Logging
            if data_source not in self._already_printed:
                self._already_printed[data_source] = 0
            if self._already_printed[data_source] < self.num_examine:
                self._already_printed[data_source] += 1
                print(f"[Reward] source={data_source} gold={gold[:80]} "
                      f"answer={str(answer)[:80]} correct={correctness:.2f} "
                      f"cost={api_cost:.2f} fmt={fmt_score} reward={reward:.3f}")

        if self.state == "train":
            return metric_tensor, cost_tensor, reward_tensor
        else:
            return metric_em_tensor, metric_f1_tensor, cost_tensor, reward_tensor, route_tensor
