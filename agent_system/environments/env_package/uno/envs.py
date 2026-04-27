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

Sub-agent: real API calls to qwen-plus via DashScope.
Every route gets a real LLM response regardless of skill choice.
The model must learn which (model, skill) combination actually works.
"""

import logging
import re
import string
import concurrent.futures
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

import gym
import numpy as np
from omegaconf import DictConfig

try:
    import openai
except ImportError:
    openai = None


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
VALID_MODELS = {
    "claude-sonnet-4-6", "claude-opus-4-6",
    "gpt-5.4", "gpt-5.3-codex",
    "gemini-3-flash-preview",
    "gemini-2.5-flash", "gemini-2.5-flash-lite",
    "kimi-k2.5",
}
VALID_SKILLS = {
    "direct_answer", "reason", "web_search", "database_query", "read_document",
    "read_code", "extract_field", "parse_structured", "symbolic_math",
    "execute_python", "execute_shell", "fact_check", "call_api",
}

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
MODEL_COST_PER_M_TOKENS = {
    # output price USD per 1M tokens (from configs/pools.yaml)
    "gemini-2.5-flash-lite": 0.40,
    "gemini-2.5-flash": 2.50,
    "kimi-k2.5": 3.00,
    "gemini-3-flash-preview": 3.00,
    "gpt-5.3-codex": 14.00,
    "gpt-5.4": 15.00,
    "claude-sonnet-4-6": 15.00,
    "claude-opus-4-6": 25.00,
}
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

# Skill → system prompt for sub-agent
SKILL_PROMPTS = {
    "direct_answer": "Answer the following question directly and concisely.",
    "reason": "Reason step by step about the following question, then give a final answer.",
    "web_search": "You are a search engine. Return the most relevant factual information for the query.",
    "symbolic_math": "You are a math solver. Compute the answer to the following math problem. Give only the numerical result.",
    "execute_python": "You are a Python executor. Write and execute Python code to solve the following. Return the output.",
    "database_query": "You are a database. Return the queried information.",
    "read_document": "You are a document reader. Extract the relevant information from the document.",
    "read_code": "You are a code analyst. Analyze the code and answer the question.",
    "extract_field": "Extract the specific field or value requested.",
    "parse_structured": "Parse the structured data and return the requested information.",
    "fact_check": "Verify the following claim and state whether it is true or false with evidence.",
    "call_api": "You are an API endpoint. Return the requested data.",
    "execute_shell": "You are a shell executor. Run the command and return the output.",
}


# Output-token budget per model tier. xiaojingai proxies every model in
# VALID_MODELS directly, so we no longer synthesize weaker models via
# qwen-plus/qwen-max — the router pays the real price (MODEL_COST_PER_M_TOKENS
# below) and sees real capability differences. max_tokens is kept modest
# so one RL rollout doesn't blow the budget cap.
_MODEL_MAX_TOKENS = {
    # lightweight tier (cheapest, short answers)
    "gemini-2.5-flash-lite": 256,
    "gemini-2.5-flash": 256,
    # mid tier
    "kimi-k2.5": 512,
    "gemini-3-flash-preview": 512,
    "claude-sonnet-4-6": 512,
    # frontier tier
    "claude-opus-4-6": 768,
    "gpt-5.4": 768,
    # code specialist
    "gpt-5.3-codex": 1024,
}
_DEFAULT_MAX_TOKENS = 256


def _get_api_client():
    """OpenAI-compatible client for the worker pool.

    Credentials come exclusively from the environment — no default key is
    baked into the source so that a forgotten `export` can never silently
    fall back to a leaked credential. `REMOTE_API_BASE` has a public
    default because the endpoint URL is not sensitive.
    """
    api_key = os.environ.get("REMOTE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "REMOTE_API_KEY is not set. Export it in the shell env before "
            "running anything that touches the worker pool."
        )
    base_url = os.environ.get(
        "REMOTE_API_BASE",
        "https://open.xiaojingai.com/v1/",
    )
    # Cap per-call latency so a single hung worker can't deadlock the whole
    # rollout's asyncio.gather. Default openai-python timeout is 600s + 2
    # auto-retries (~30 min worst case) — that masks as a "step 2 hang" in
    # GRPO. 60s + 1 retry caps a single sub-agent call at ~120s.
    return openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=60.0,
        max_retries=1,
    )


def call_sub_agent_api(model: str, skill: str, query: str, question: str) -> Tuple[str, int]:
    """Call the routed worker model via xiaojingai and return (text, out_tokens).

    The router's own choice of `model` is sent verbatim (no tier remap),
    so the routed model really runs — cost and quality
    differences are authentic. Skill picks the worker's system prompt;
    the user turn carries the original task question (for context) plus
    the planner's specific subtask query.
    """
    client = _get_api_client()
    sys_prompt = SKILL_PROMPTS.get(skill, "Answer the following question concisely.")
    max_tok = _MODEL_MAX_TOKENS.get(model, _DEFAULT_MAX_TOKENS)

    user_content = (
        f"Original question: {question}\n\nSub-task: {query}\n\nAnswer directly, no chain of thought."
        if question
        else f"Sub-task: {query}\n\nAnswer directly, no chain of thought."
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tok,
            temperature=0.3,
        )
        text = resp.choices[0].message.content.strip()
        output_tokens = resp.usage.completion_tokens
        return text, output_tokens
    except Exception as e:
        return f"API error: {str(e)[:200]}", 0


# Hard wall-clock timeout for a single sub-agent call. The OpenAI sync
# client is configured with timeout=60s + max_retries=1, so a healthy
# call should top out around ~120s. We give a small margin (150s) and
# treat anything beyond as a silent hang (already-observed failure mode:
# step 4 of verify_10step_fix_20260427_213203 stalled >10 min with all
# GPUs idle and no exception ever raised by the SDK — see
# project_format_gate_false_positive memory and _hang_evidence/).
_SUBAGENT_HARD_TIMEOUT_SEC = float(os.environ.get("UNO_SUBAGENT_HARD_TIMEOUT_SEC", "150"))
# Sized for the rollout concurrency (verl agent loop runs ~8 worker
# processes × tens of concurrent env.step threads). When a wrapped
# call hangs at the HTTP layer the underlying thread is leaked until
# the OS-level TCP timeout fires; with a generous pool the leak is
# absorbed for a 10-step verify and beyond.
_SUBAGENT_TIMEOUT_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("UNO_SUBAGENT_TIMEOUT_POOL_SIZE", "256")),
    thread_name_prefix="uno-subagent-timeout",
)


def call_sub_agent_api_with_hard_timeout(
    model: str, skill: str, query: str, question: str
) -> Tuple[str, int, bool]:
    """Wrap call_sub_agent_api with an outer wall-clock timeout.

    Returns (text, output_tokens, hard_timed_out). On hard-timeout we
    log a WARNING (so frequency is observable in the agent loop log)
    and return a sentinel obs string with 0 tokens. The caller must
    treat hard_timed_out=True as a fatal-for-this-rollout signal:
    terminate the episode, surface metadata["timeout"]=True so the
    rollout's done_reason becomes "timeout" and env_terminal_reward
    becomes 0 via the standard terminal-reward path.

    The wrapped openai SDK call still runs in the background after we
    return — that thread is leaked until the OS-level TCP timeout
    eventually fires. This is intentional: the alternative (blocking
    on the call until it finishes) is exactly the deadlock we're
    fixing. The pool is sized generously to absorb the rare leak.
    """
    fut = _SUBAGENT_TIMEOUT_POOL.submit(
        call_sub_agent_api, model, skill, query, question
    )
    try:
        text, output_tokens = fut.result(timeout=_SUBAGENT_HARD_TIMEOUT_SEC)
        return text, output_tokens, False
    except concurrent.futures.TimeoutError:
        logger.warning(
            "uno sub-agent hard timeout after %.1fs (model=%s skill=%s "
            "query=%r) — terminating rollout with done_reason=timeout. "
            "Underlying thread leaked, will reap when TCP timeout fires.",
            _SUBAGENT_HARD_TIMEOUT_SEC, model, skill, query[:80],
        )
        return (
            f"<error reason=\"sub_agent_hard_timeout\" after_s=\"{_SUBAGENT_HARD_TIMEOUT_SEC}\"/>",
            0,
            True,
        )


from agent_system.environments.env_package.uno.verifiers import verify as _verify_by_source


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

        # Parse routes and call real API
        routes = ROUTE_RE.findall(action)
        if routes:
            obs_parts = []
            for round_n, subtask_id, model, skill, query in routes:
                # Call real API with an outer hard wall-clock timeout.
                # On silent SDK hang we terminate THIS rollout immediately
                # rather than blocking the whole batch's asyncio.gather.
                response_text, output_tokens, hard_timed_out = \
                    call_sub_agent_api_with_hard_timeout(
                        model, skill, query, self.question
                    )

                if hard_timed_out:
                    obs_parts.append(
                        f'<obs subtask="{subtask_id}">{response_text}</obs>'
                    )
                    self.done = True
                    reward = 0.0
                    metadata["timeout"] = True
                    metadata["api_hard_timeout"] = True
                    break

                self.total_output_tokens += output_tokens

                # Compute cost using the ROUTED model's pricing (not qwen-plus actual cost)
                # This is the cost the Router "would have paid" if using the real model
                cost_per_m = MODEL_COST_PER_M_TOKENS.get(model, 10.0)
                self.total_api_cost += cost_per_m * max(output_tokens, 1) / 1e6

                obs_parts.append(f'<obs subtask="{subtask_id}">{response_text}</obs>')
            observations = [{"content": "\n".join(obs_parts)}]

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
    """Vectorized UNO environment with real API calls."""

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
        # More workers for parallel API calls
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
