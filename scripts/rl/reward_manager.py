"""
SkillRouter reward manager for GRPO training.

Reward: R = (1 - α) * R_correctness + α * R_cost
  - R_correctness: per-source verifier (math/qa/code/toolace)
  - R_cost: sqrt + rolling-percentile-normalised API cost (1 = cheapest, 0 = most expensive)
  - Terminal-only: wrong / invalid / incomplete → 0.0 (no format penalty —
    SFT already taught the schema, so we don't double-dip).
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

# ── Rolling-percentile cost normalisation (Router-R1 style) ──────────
# Following Router-R1 (main_ppo.py:41-72): instead of dividing by a hard-
# coded budget cap, we keep a rolling buffer of recent per-episode costs,
# sqrt-transform them to compress the long tail (Opus is ~100× Flash),
# and percentile-normalise so that the 5-95% band maps to [0, 1] with
# cheap → 1 and expensive → 0. This is robust to outliers (one runaway
# Opus rollout no longer saturates the signal forever) and removes the
# magic BUDGET_CAP knob.
_COST_WINDOW_SIZE = 1000
_COST_Q_LOW, _COST_Q_HIGH = 0.05, 0.95
_COST_TRANSFORM = "sqrt"   # {"sqrt", "log", "none"}
_COST_LOG_ALPHA = 0.01     # only used when _COST_TRANSFORM == "log"
_COST_EPS = 1e-8
_cost_buffer: deque = deque(maxlen=_COST_WINDOW_SIZE)


def _preprocess_cost(c: float) -> float:
    if _COST_TRANSFORM == "sqrt":
        return float(np.sqrt(max(c, 0.0)))
    if _COST_TRANSFORM == "log":
        return float(np.log1p(_COST_LOG_ALPHA * max(c, 0.0)))
    return float(max(c, 0.0))


def _rolling_percentile_cost_reward(raw_cost: float) -> float:
    """Router-R1-style cost reward in [0, 1]. Cheaper → higher."""
    r = _preprocess_cost(raw_cost)
    _cost_buffer.append(r)
    arr = np.asarray(_cost_buffer, dtype=np.float32)
    if arr.size >= 2:
        r_min = float(np.percentile(arr, 100 * _COST_Q_LOW))
        r_max = float(np.percentile(arr, 100 * _COST_Q_HIGH))
    else:
        r_min, r_max = float(arr.min()), float(arr.max())
    denom = r_max - r_min
    if denom < _COST_EPS:
        return 0.5  # buffer too narrow to distinguish cheap vs expensive
    return 1.0 - float(np.clip((r - r_min) / denom, 0.0, 1.0))


_ROUTE_RE = re.compile(
    r'<route\s+round="\d+"\s+subtask="\d+"\s+model="([^"]+)"\s+skill="[^"]+"\s*>'
    r"(.*?)</route>",
    re.DOTALL,
)


def _extract_routes(completion: str) -> list[dict]:
    """Pull every `<route round="N" subtask="K" model="M" skill="S">query</route>`
    from a rollout, returning {"model": M, "instruction": query}.
    Matches the SFT schema v6; used by `estimate_cost` to price the
    trajectory at reward time.
    """
    return [
        {"model": model.strip(), "instruction": query.strip()}
        for model, query in _ROUTE_RE.findall(completion)
    ]


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

    Uses Router-R1's rolling-percentile normalisation (see
    `_rolling_percentile_cost_reward`). No hardcoded budget cap — the
    buffer adapts to the cost distribution seen during training so the
    same α weight works across different worker-pool mixes.
    """
    cost = estimate_cost(completion, tokenizer)
    return _rolling_percentile_cost_reward(cost)


def normalize_cost(raw_cost: float) -> float:
    """Legacy interface — wraps the rolling-percentile normaliser directly."""
    try:
        c = float(raw_cost)
    except (TypeError, ValueError):
        return 0.5
    return _rolling_percentile_cost_reward(c)


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


# ── Per-source verifier routing ───────────────────────────────
# Each entry maps a `source` value (from the RL parquet's extra_info)
# to the binary verifier to use on its final answer. Categories that
# don't need special handling fall back to token-level F1.

_MATH_SOURCES = {
    "gsm8k", "gsm8k_main", "numinamath",
    "hendrycks_math_algebra", "hendrycks_math_intermediate_algebra",
    "hendrycks_math_number_theory", "aqua_rat", "theoremqa",
}
_CODE_SOURCES = {"taco", "codecontests", "codeforces_cots"}
_TOOL_SOURCES = {"toolace"}
# Everything else (qa_*, reading_comprehension, reasoning_commonsense,
# knowledge_academic, etc.) falls through to F1 below.


def _taco_verify(pred: str, gold: str, question: str, tests: dict | None) -> float:
    """Score a TACO/code rollout. Prefer the oracle test cases from
    `env_kwargs`; fall back to the subprocess executor when missing.
    Reuses the env-side PRIME harness so Path A (GiGPO) and Path B (GRPO)
    grade code identically at train time.
    """
    if isinstance(tests, dict) and tests.get("inputs") and tests.get("outputs"):
        try:
            from agent_system.environments.env_package.skillrouter.verifiers.code_verifier import (
                verify_code as _prime_verify,
            )
            return 1.0 if _prime_verify(pred, gold, tests=tests) else 0.0
        except Exception:
            pass
    return verify_code_exec(pred, gold, question)


def compute_correctness(pred: str, gold: str, source: str,
                        question: str = "", tests: dict | None = None) -> float:
    """Binary correctness score for a rollout, routed by `source`."""
    key = source.lower() if isinstance(source, str) else ""
    if key in _CODE_SOURCES:
        return _taco_verify(pred, gold, question, tests)
    if key in _MATH_SOURCES:
        return float(verify_math(pred, gold))
    if key in _TOOL_SOURCES:
        return float(verify_toolace(pred, gold))
    return _f1_score(pred, gold)


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

            # 1. Extract answer
            answer = extract_answer(completion)

            # 2. Cost reward (from trajectory text, tokenizer for exact count)
            cost_r = compute_cost_reward(completion, self.tokenizer)

            # 3. Route count
            n_routes = route_count(completion)

            # 4. Compute correctness — verifier is routed by `source`
            # (the actual HF dataset name), not by `data_source` which is
            # the broader category. prepare_prompt_pool stashes the source
            # + per-sample TACO test cases in extra_info.
            extra_info = data_item.non_tensor_batch.get('extra_info', '{}')
            if isinstance(extra_info, str):
                try:
                    extra_info = json.loads(extra_info)
                except Exception:
                    extra_info = {}
            question = extra_info.get('question', '')
            source = extra_info.get('source', data_source)  # fall back to category
            tests = extra_info.get('tests')                 # None for non-code

            if answer is None:
                correctness = 0.0
                em_score = 0.0
                f1_score_val = 0.0
            else:
                correctness = compute_correctness(
                    answer, gold, source, question, tests=tests
                )
                em_score = _em_score(answer, gold)
                f1_score_val = _f1_score(answer, gold)

            # 5. Terminal-only reward. No format penalty — SFT already
            #    taught the schema; a malformed output simply can't yield
            #    a correct answer, so it collapses to correctness == 0.
            #   wrong / invalid / no answer → 0.0
            #   correct                      → (1-α) × correctness + α × cost_reward
            if correctness == 0:
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
                print(f"[Reward] source={data_source} gold={str(gold)[:80]} "
                      f"answer={str(answer)[:80]} correct={correctness:.2f} "
                      f"cost_r={cost_r:.2f} reward={reward:.3f}")

        if self.state == "train":
            return metric_tensor, cost_tensor, reward_tensor
        else:
            return metric_em_tensor, metric_f1_tensor, cost_tensor, reward_tensor, route_tensor
