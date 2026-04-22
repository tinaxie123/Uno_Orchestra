import re
import string
import concurrent.futures
import os
import sys
from typing import Any, Dict, List, Tuple

import gym
import numpy as np
from omegaconf import DictConfig

try:
    import openai
except ImportError:
    openai = None
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "../../../../..")))
from configs import load_pools as _load_pools

_pools = _load_pools()
PLAN_RE = re.compile(r'<plan round="(\d+)">(.*?)</plan>', re.DOTALL)
SUBTASK_RE = re.compile(r'<subtask id="(\d+)" depends_on="([^"]*)">(.*?)</subtask>', re.DOTALL)
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

VALID_MODELS = set(_pools["models"])
VALID_SKILLS = set(_pools["skills"])

MODEL_COST_PER_M_TOKENS = _pools["cost_per_m"]
DEFAULT_ALPHA = 0.1
COST_WINDOW_SIZE = 1000  
COST_PERCENTILE_LO = 5
COST_PERCENTILE_HI = 95

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


ROUTER_TO_DASHSCOPE = {
   
    "claude-haiku-4-5-20251001": ("qwen-plus", 64),
    "gemini-2.5-flash": ("qwen-plus", 64),
    "kimi-k2.5": ("qwen-plus", 256),
    "claude-sonnet-4-6": ("qwen-plus", 256),
    "gemini-3.1-pro-preview": ("qwen-plus", 256),
    "qwen3.6-plus": ("qwen-plus", 256),
    "claude-opus-4-6": ("qwen-max", 512),
    "gpt-5.4": ("qwen-max", 512),
    "gpt-5.3-codex": ("qwen-plus", 512),
}


def _get_api_client():
    """Get or create the DashScope API client."""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "sk-70793c3ec75a40ca90b7076de9927260")
    base_url = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def call_sub_agent_api(model: str, skill: str, query: str, question: str) -> Tuple[str, int]:
    """
    Call real LLM API as sub-agent with model-tier mapping.
    Different routed models → different DashScope models → different quality answers.
    Returns (response_text, output_tokens).
    """
    client = _get_api_client()
    sys_prompt = SKILL_PROMPTS.get(skill, "Answer the following question concisely.")
    actual_model, max_tok = ROUTER_TO_DASHSCOPE.get(model, ("qwen-plus", 256))

    try:
        resp = client.chat.completions.create(
            model=actual_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Original question: {question}\n\nSub-task: {query}\n\nAnswer directly, no chain of thought."},
            ],
            max_tokens=max_tok,
            temperature=0.3,
            extra_body={"enable_thinking": False},
        )
        text = resp.choices[0].message.content.strip()
        output_tokens = resp.usage.completion_tokens
        return text, output_tokens
    except Exception as e:
        return f"API error: {str(e)[:100]}", 0


def normalize_answer(s: str) -> str:
    s = s.lower().strip()
    for art in ("a ", "an ", "the "):
        if s.startswith(art):
            s = s[len(art):]
    s = s.translate(str.maketrans("", "", string.punctuation))
    s = " ".join(s.split())
    return s


def check_correctness(prediction: str, gold: str) -> float:
    if not prediction or not gold:
        return 0.0
    pred_norm = normalize_answer(prediction)
    gold_norm = normalize_answer(gold)
    if pred_norm == gold_norm:
        return 1.0
    if gold_norm in pred_norm or pred_norm in gold_norm:
        return 1.0
    return 0.0


class SingleSkillRouterEnv:
    """Single environment instance for one (question, gold) pair."""

    def __init__(self):
        self.question = None
        self.gold = None
        self.data_source = None
        self.max_turns = 3
        self.current_round = 0
        self.total_api_cost = 0.0
        self.total_output_tokens = 0
        self.done = False
        self.final_answer = None

    def reset(self, extras: Dict):
        self.question = extras["question"]
        self.gold = extras["ground_truth"]
        self.data_source = extras.get("data_source", "unknown")
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

        # Format validation: must contain <final_answer> OR (<plan> + <route>)
        has_final = bool(FINAL_RE.search(action))
        has_plan = bool(PLAN_RE.search(action))
        has_route = bool(ROUTE_RE.search(action))
        format_valid = has_final or (has_plan and has_route)
        metadata["format_valid"] = format_valid

        if not format_valid:
            # Invalid format → done, no reward
            self.done = True
            metadata["format_error"] = True
            return {
                "observations": observations,
                "reward": 0.0,
                "done": True,
                "metadata": metadata,
            }

        # Check for final_answer
        final_match = FINAL_RE.search(action)
        if final_match:
            self.final_answer = final_match.group(1).strip()
            self.done = True
            reward = check_correctness(self.final_answer, self.gold)
            metadata["correctness"] = reward
            metadata["final_answer"] = self.final_answer
            metadata["format_valid"] = True
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
                # Call real API
                response_text, output_tokens = call_sub_agent_api(
                    model, skill, query, self.question
                )
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


class SkillRouterMultiProcessEnv(gym.Env):
    """Vectorized SkillRouter environment with real API calls."""

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
        self.alpha = env_config.get("alpha", DEFAULT_ALPHA) if env_config else DEFAULT_ALPHA

        # Rolling cost buffer for sqrt + percentile normalization (Router-R1 style)
        self._cost_buffer: list[float] = []

        self.envs = [SingleSkillRouterEnv() for _ in range(self.batch_size)]
        # More workers for parallel API calls
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.batch_size, 128)
        )

    def reset(self, kwargs: List[Dict]) -> Tuple[List[str], List[Dict]]:
        if len(kwargs) > self.batch_size:
            self.batch_size = len(kwargs)
            self.envs = [SingleSkillRouterEnv() for _ in range(self.batch_size)]

        obs_list = []
        info_list = []
        for i, kw in enumerate(kwargs):
            extras = {
                "question": kw["question"],
                "ground_truth": kw["ground_truth"],
                "data_source": kw.get("data_source", "unknown"),
                "max_turns": self.max_steps,
            }
            self.envs[i].reset(extras)
            obs_list.append(kw["question"])
            info_list.append({"data_source": kw.get("data_source", "unknown")})

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

            # Router-R1 hierarchical reward:
            # R = R_format + (1-α)*R_outcome + α*R_cost
            # If format invalid → R_outcome and R_cost are nullified
            correctness = result["reward"]
            is_valid = result.get("metadata", {}).get("format_valid", True)

            if not is_valid:
                # Format reward = -1, nullify everything else
                rewards[i] = -1.0
            else:
                # Format OK → compute outcome + cost
                # Router-R1 style: sqrt + rolling percentile normalization
                api_cost = self.envs[i].total_api_cost
                sqrt_cost = np.sqrt(api_cost) if api_cost > 0 else 0.0
                self._cost_buffer.append(sqrt_cost)
                if len(self._cost_buffer) > COST_WINDOW_SIZE:
                    self._cost_buffer = self._cost_buffer[-COST_WINDOW_SIZE:]

                if len(self._cost_buffer) >= 10:
                    lo = np.percentile(self._cost_buffer, COST_PERCENTILE_LO)
                    hi = np.percentile(self._cost_buffer, COST_PERCENTILE_HI)
                    if hi > lo:
                        norm_cost = np.clip((sqrt_cost - lo) / (hi - lo), 0.0, 1.0)
                    else:
                        norm_cost = 0.5
                else:
                  
                    norm_cost = 0.5

                r_cost = 1.0 - norm_cost  
                rewards[i] = (1 - self.alpha) * correctness + self.alpha * r_cost

            dones[i] = result["done"]
            info = result.get("metadata", {})
            info["data_source"] = self.envs[i].data_source
            info["won"] = bool(correctness >= 1.0)
            info["format_valid"] = is_valid
            infos.append(info)

        return next_obs, rewards, dones, infos

    def close(self):
        self._executor.shutdown(wait=False)
