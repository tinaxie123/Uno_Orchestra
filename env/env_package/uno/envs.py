"""
UNO Environment for verl-agent.

Each episode:
1. Model receives a question
2. Model generates <plan> + <route> tags (assistant turn)
3. Environment parses routes, calls real LLM API as sub-agent, returns <obs>
4. Model generates <verify> + optionally <final_answer> or repair <plan>
5. Repeat until <final_answer> or max_steps

Reward: R = (1-α)·R_outcome + α·R_cost (outcome ∈ {0,1}, R_cost from
rolling-percentile winsorisation of sqrt-transformed API cost)

Sub-agent: routes are dispatched through the closed primitive pool in
primitives.py. Some primitives use bounded local backends, others call the
routed worker model with a primitive-specific prompt.
"""

import logging
import re
import string
import concurrent.futures
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

import gym
import numpy as np
from omegaconf import DictConfig

from uno_orchestor.routing.uno.harness import (
    RouteValidationError,
    build_default_harness,
    load_harness_config,
)
from uno_orchestor.routing.uno.primitives import Route


# --- Schema v1.1 Parsers ---
PLAN_RE = re.compile(r'<plan round="(\d+)">(.*?)</plan>', re.DOTALL)
# Subtask tags are parsed by the SFT model but carry no runtime meaning:
# depends_on is informational only, routes in the same <plan round="N">
# are dispatched in parallel. (If the model needs sequencing, it should
# emit the dependent subtask in <plan round="N+1">.)
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

# Valid pools
_HARNESS_CONFIG = load_harness_config()
VALID_MODELS = set(_HARNESS_CONFIG.valid_models)
VALID_SKILLS = set(_HARNESS_CONFIG.valid_skills)
MODEL_SKILLS = {model: set(skills) for model, skills in _HARNESS_CONFIG.model_skills.items()}

# Sources where a "lazy" direct-answer (no <plan>/<route>) is legitimate
# — atomic reasoning / single-hop knowledge questions the router should
# learn to NOT decompose. Everything else (multi-hop QA, code, tool,
# competition math, reading comprehension) must emit at least one
# <plan>+<route> before a <final_answer>; otherwise the episode would
# collapse to a 1-turn rollout and never exercise multi-turn routing.
LAZY_ALLOWED_SOURCES = {
    "gsm8k", "gsm8k_main",
    "commonsenseqa", "arc_challenge", "piqa", "social_iqa", "winogrande",
    "openbookqa", "mmlu_aux_stem", "sciq", "aqua_rat",
    "strategyqa", "logiqa2", "folio",
    "bbh_formal_fallacies", "bbh_logical_deduction",
}

# Per-token cost (USD per 1M output tokens)
MODEL_COST_PER_M_TOKENS = dict(_HARNESS_CONFIG.cost_per_m_tokens)
_DEFAULT_HARNESS = build_default_harness()
# ── Rolling-percentile cost normalisation ───────────────────────────
# Cost normalisation without a hand-tuned budget cap. Raw USD cost is
# sqrt-transformed first (compressing the ~100× dynamic range between
# frontier models like Opus and cheap ones like Flash-Lite into a more
# linear scale), then winsorised against the 5-95% band of a rolling
# buffer of recent episodes: the cheapest 5% map to 1.0, the most
# expensive 5% map to 0.0, the rest interpolates linearly. This yields
# cheap → high reward / expensive → low reward without introducing a
# magic-number BUDGET_CAP hyperparameter, and the buffer makes the
# signal robust to single-episode outliers.
_COST_WINDOW_SIZE = 1000
_COST_Q_LOW, _COST_Q_HIGH = 0.05, 0.95
_COST_EPS = 1e-8
_cost_buffer: List[float] = []


def _rolling_percentile_cost_reward(raw_cost: float) -> float:
    """Cost reward in [0, 1]. Cheaper → higher (sqrt-compressed,
    winsorised against a rolling buffer of recent episode costs).
    """
    r = float(np.sqrt(max(raw_cost, 0.0)))
    _cost_buffer.append(r)
    if len(_cost_buffer) > _COST_WINDOW_SIZE:
        del _cost_buffer[: len(_cost_buffer) - _COST_WINDOW_SIZE]
    arr = np.asarray(_cost_buffer, dtype=np.float32)
    if arr.size >= 2:
        r_min = float(np.percentile(arr, 100 * _COST_Q_LOW))
        r_max = float(np.percentile(arr, 100 * _COST_Q_HIGH))
    else:
        r_min, r_max = float(arr.min()), float(arr.max())
    denom = r_max - r_min
    if denom < _COST_EPS:
        return 0.5
    return 1.0 - float(np.clip((r - r_min) / denom, 0.0, 1.0))

from env.env_package.uno.verifiers import verify as _verify_by_source


def check_correctness(prediction: str, gold: str, source: str = "", extras: dict | None = None) -> float:
    """Route to the per-source verifier (math / qa / code / toolace).

    `source` carries the specific benchmark name (hotpotqa, taco, numinamath, ...)
    and selects the correct verifier. `extras` may carry per-task artifacts
    (e.g. code tests).
    """
    if not prediction or not gold:
        return 0.0
    try:
        return float(_verify_by_source(prediction, gold, source or "", extras or {}))
    except Exception:
        return 0.0


class SingleUnoEnv:
    """Single environment instance for one (question, gold) pair."""

    def __init__(self):
        self.question = None
        self.gold = None
        self.data_source = None
        self.source = None
        self.tests = None
        # default aligned with rollout_loop's max_steps=5 so the env
        # doesn't force-done before the RL loop's iteration budget runs
        # out. env_manager overrides this via extras["max_turns"].
        self.max_turns = 5
        self.current_round = 0
        self.total_api_cost = 0.0
        self.total_output_tokens = 0
        self.done = False
        self.final_answer = None
        self.harness = _DEFAULT_HARNESS

    def reset(self, extras: Dict):
        self.question = extras["question"]
        self.gold = extras["ground_truth"]
        self.data_source = extras.get("data_source", "unknown")
        self.source = extras.get("source", "") or self.data_source
        self.tests = extras.get("tests")
        self.max_turns = extras.get("max_turns", 3)
        self.current_round = 0
        self.total_api_cost = 0.0
        self.total_output_tokens = 0
        self.done = False
        self.final_answer = None

    def step(self, action: str) -> Dict:
        self.current_round += 1
        observations = []
        reward = 0.0
        metadata = {}

        # Format validation.  We accept three shapes, matching what the
        # SFT model actually emits:
        #   (a) explicit <final_answer>...</final_answer>    — terminal
        #   (b) <plan>+<route>                                — routing round
        #   (c) lazy mode: assistant turn has no <plan>/<route>/<final_answer>
        #       but is a direct natural-language answer (common for simple
        #       QA after SFT). Treat the whole turn as the answer.
        has_final = bool(FINAL_RE.search(action))
        has_plan = bool(PLAN_RE.search(action))
        has_route = bool(ROUTE_RE.search(action))
        is_lazy = (not has_final) and (not has_plan) and (not has_route) and bool(action.strip())

        # Lazy-mode is only legitimate for atomic-reasoning sources the
        # router should learn to NOT decompose. For multi-hop / code /
        # tool / competition-math, require routing before accepting a
        # final answer — otherwise the episode collapses to 1 turn and
        # the RL loop never exercises env feedback.
        src_key = (self.source or "").lower()
        lazy_allowed = src_key in LAZY_ALLOWED_SOURCES
        if is_lazy and not lazy_allowed:
            is_lazy = False
            metadata["lazy_rejected"] = True
        # Only reject "<final_answer> without plan/route" on the FIRST
        # turn. By round 2+, the env has already executed at least one
        # routing turn (otherwise the episode would have been done after
        # round 1's terminal/format_error path), so a turn-2 message that
        # only contains <verify> + <final_answer> is the legitimate
        # "synthesise-after-routing" pattern, not a lazy bypass. Without
        # this guard, schema-perfect multi-turn rollouts get mis-flagged
        # format_error and the gradient signal collapses (see verify_10step
        # canary: 60%+ format_error driven entirely by this false positive).
        already_routed = self.current_round > 1
        if (
            has_final
            and not (has_plan or has_route)
            and not lazy_allowed
            and not already_routed
        ):
            has_final = False
            metadata["lazy_rejected"] = True

        format_valid = has_final or (has_plan and has_route) or is_lazy
        metadata["format_valid"] = format_valid

        if not format_valid:
            # Truly empty / garbage → done, no reward
            self.done = True
            metadata["format_error"] = True
            return {
                "observations": observations,
                "reward": 0.0,
                "done": True,
                "metadata": metadata,
            }

        # Terminal paths: explicit <final_answer> OR lazy direct-answer
        if has_final or is_lazy:
            if has_final:
                final_match = FINAL_RE.search(action)
                self.final_answer = final_match.group(1).strip()
            else:
                # Lazy mode: trim any stray trailing chat-template tokens
                self.final_answer = action.strip().rstrip("<|im_end|>").strip()
            self.done = True
            reward = check_correctness(
                self.final_answer, self.gold, self.source,
                extras={"tests": self.tests} if self.tests else None,
            )
            metadata["correctness"] = reward
            metadata["source"] = self.source
            metadata["final_answer"] = self.final_answer
            metadata["format_valid"] = True
            metadata["lazy_mode"] = is_lazy
            return {
                "observations": observations,
                "reward": reward,
                "done": True,
                "metadata": metadata,
            }

        # Parse routes and dispatch through the route harness.
        routes = ROUTE_RE.findall(action)
        if routes:
            obs_parts = []
            primitive_backends = []
            for round_n, subtask_id, model, skill, query in routes:
                route = Route(
                    round=int(round_n),
                    subtask=int(subtask_id),
                    model=model,
                    skill=skill,
                    query=query,
                )
                try:
                    primitive_result = self.harness.run_route(route, self.question)
                except RouteValidationError as exc:
                    route_error = str(exc)
                    obs_parts.append(
                        f'<obs subtask="{subtask_id}"><error reason="invalid_route">{route_error}</error></obs>'
                    )
                    self.done = True
                    reward = 0.0
                    metadata["format_error"] = True
                    metadata["invalid_route"] = route_error
                    break
                response_text = primitive_result.text
                primitive_backends.append(primitive_result.backend)

                if primitive_result.timed_out:
                    obs_parts.append(
                        f'<obs subtask="{subtask_id}">{response_text}</obs>'
                    )
                    self.done = True
                    reward = 0.0
                    metadata["timeout"] = True
                    metadata["api_hard_timeout"] = True
                    break

                self.total_output_tokens += primitive_result.output_tokens

                if primitive_result.billable:
                    # Compute cost using the ROUTED model's pricing.
                    cost_per_m = MODEL_COST_PER_M_TOKENS.get(model, 10.0)
                    self.total_api_cost += (
                        cost_per_m * max(primitive_result.output_tokens, 1) / 1e6
                    )

                obs_parts.append(f'<obs subtask="{subtask_id}">{response_text}</obs>')
            observations = [{"content": "\n".join(obs_parts)}]
            metadata["primitive_backends"] = primitive_backends

        # Max turns exceeded
        if self.current_round >= self.max_turns:
            self.done = True
            reward = 0.0
            metadata["timeout"] = True

        metadata["round"] = self.current_round
        metadata["n_routes"] = len(routes)

        return {
            "observations": observations,
            "reward": reward,
            "done": self.done,
            "metadata": metadata,
        }


class UnoMultiProcessEnv(gym.Env):
    """Vectorized UNO environment with harness-backed primitive dispatch."""

    def __init__(
        self,
        seed: int = 0,
        env_num: int = 1,
        group_n: int = 1,
        is_train: bool = True,
        env_config: DictConfig | None = None,
    ):
        super().__init__()
        self.env_num = env_num
        self.group_n = group_n
        self.batch_size = env_num * group_n
        self.is_train = is_train
        self.max_steps = env_config.max_steps if env_config else 3
        self.alpha = env_config.get("alpha", 0.1) if env_config else 0.1

        self.envs = [SingleUnoEnv() for _ in range(self.batch_size)]
        # More workers for parallel environment steps.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.batch_size, 128)
        )

    def reset(self, kwargs: List[Dict]) -> Tuple[List[str], List[Dict]]:
        if len(kwargs) > self.batch_size:
            self.batch_size = len(kwargs)
            self.envs = [SingleUnoEnv() for _ in range(self.batch_size)]

        obs_list = []
        info_list = []
        for i, kw in enumerate(kwargs):
            extras = {
                "question": kw["question"],
                "ground_truth": kw["ground_truth"],
                "data_source": kw.get("data_source", "unknown"),
                "source": kw.get("source", "") or kw.get("data_source", "unknown"),
                "tests": kw.get("tests"),
                "max_turns": self.max_steps,
            }
            self.envs[i].reset(extras)
            obs_list.append(kw["question"])
            info_list.append({
                "data_source": kw.get("data_source", "unknown"),
                "source": kw.get("source", "") or kw.get("data_source", "unknown"),
            })

        return obs_list, info_list

    def step(self, actions: List[str]) -> Tuple[List[str], np.ndarray, np.ndarray, List[Dict]]:
        # Parallel API calls via thread pool
        results = list(self._executor.map(
            lambda args: args[0].step(args[1]),
            zip(self.envs, actions)
        ))

        next_obs = []
        rewards = np.zeros(len(actions), dtype=np.float32)
        dones = np.zeros(len(actions), dtype=bool)
        infos = []

        for i, result in enumerate(results):
            obs_content = ""
            if result["observations"]:
                obs_content = result["observations"][0]["content"]
            next_obs.append(obs_content)

            # Outcome-only reward:
            #   mid-step                 → 0.0
            #   terminal, answer wrong   → 0.0   (includes malformed output —
            #                                     SFT has already taught the
            #                                     format, so we don't double-
            #                                     dip with a format penalty;
            #                                     a bad trajectory simply can't
            #                                     produce a correct answer)
            #   terminal, answer correct → (1-α)·1 + α·(1 - cost/budget)
            correctness = result["reward"]              # 0 / 1 (nonzero only on final_answer)
            is_valid = result.get("metadata", {}).get("format_valid", True)
            done = result["done"]

            if not done or correctness <= 0:
                rewards[i] = 0.0
            else:
                api_cost = self.envs[i].total_api_cost
                r_cost = _rolling_percentile_cost_reward(api_cost)
                rewards[i] = (1 - self.alpha) * correctness + self.alpha * r_cost

            dones[i] = done
            info = result.get("metadata", {})
            info["data_source"] = self.envs[i].data_source
            info["won"] = bool(correctness >= 1.0)
            info["format_valid"] = is_valid
            infos.append(info)

        return next_obs, rewards, dones, infos

    def close(self):
        self._executor.shutdown(wait=False)
