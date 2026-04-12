"""
SkillRouter Environment for verl-agent.

Each episode:
1. Model receives a question
2. Model generates <plan> + <route> tags (assistant turn)
3. Environment parses routes, calls real LLM API as sub-agent, returns <obs>
4. Model generates <verify> + optionally <final_answer> or repair <plan>
5. Repeat until <final_answer> or max_steps

Reward: Router-R1 style R = (1-α)*R_outcome + α*R_cost

Sub-agent: real API calls to qwen-plus via DashScope.
Every route gets a real LLM response regardless of skill choice.
The model must learn which (model, skill) combination actually works.
"""

import re
import string
import concurrent.futures
import os
from typing import Any, Dict, List, Tuple

import gym
import numpy as np
from omegaconf import DictConfig

try:
    import openai
except ImportError:
    openai = None


# --- Schema v1.1 Parsers ---
PLAN_RE = re.compile(r'<plan round="(\d+)">(.*?)</plan>', re.DOTALL)
SUBTASK_RE = re.compile(r'<subtask id="(\d+)" depends_on="([^"]*)">(.*?)</subtask>', re.DOTALL)
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

# Valid pools
VALID_MODELS = {
    "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6",
    "gpt-5.4", "gpt-5.3-codex", "gemini-3.1-pro-preview", "gemini-2.5-flash",
    "kimi-k2.5", "qwen3.6-plus",
}
VALID_SKILLS = {
    "direct_answer", "reason", "web_search", "database_query", "read_document",
    "read_code", "extract_field", "parse_structured", "symbolic_math",
    "execute_python", "execute_shell", "fact_check", "call_api",
}

# Per-token cost (USD per 1M output tokens)
MODEL_COST_PER_M_TOKENS = {
    "claude-haiku-4-5-20251001": 1.25,
    "gemini-2.5-flash": 1.50,
    "kimi-k2.5": 2.00,
    "claude-sonnet-4-6": 15.00,
    "gemini-3.1-pro-preview": 10.00,
    "gpt-5.3-codex": 20.00,
    "qwen3.6-plus": 8.00,
    "claude-opus-4-6": 75.00,
    "gpt-5.4": 60.00,
}
MAX_COST_PER_ROUTE = MODEL_COST_PER_M_TOKENS["claude-opus-4-6"] * 500 / 1e6
MAX_EPISODE_COST = MAX_COST_PER_ROUTE * 8
DEFAULT_ALPHA = 0.1

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


def _get_api_client():
    """Get or create the DashScope API client."""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "sk-70793c3ec75a40ca90b7076de9927260")
    base_url = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def call_sub_agent_api(skill: str, query: str, question: str) -> Tuple[str, int]:
    """
    Call real LLM API as sub-agent.
    Returns (response_text, output_tokens).
    No CoT — direct answer only.
    """
    client = _get_api_client()
    sys_prompt = SKILL_PROMPTS.get(skill, "Answer the following question concisely.")

    try:
        resp = client.chat.completions.create(
            model="qwen-plus",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Original question: {question}\n\nSub-task: {query}\n\nAnswer directly, no chain of thought."},
            ],
            max_tokens=256,
            temperature=0.3,
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

        # Check for final_answer
        final_match = FINAL_RE.search(action)
        if final_match:
            self.final_answer = final_match.group(1).strip()
            self.done = True
            reward = check_correctness(self.final_answer, self.gold)
            metadata["correctness"] = reward
            metadata["final_answer"] = self.final_answer
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
                    skill, query, self.question
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

            # Router-R1 reward
            correctness = result["reward"]
            api_cost = self.envs[i].total_api_cost
            r_cost = 1.0 - min(api_cost / MAX_EPISODE_COST, 1.0) if MAX_EPISODE_COST > 0 else 1.0
            rewards[i] = (1 - self.alpha) * correctness + self.alpha * r_cost

            dones[i] = result["done"]
            info = result.get("metadata", {})
            info["data_source"] = self.envs[i].data_source
            info["won"] = bool(correctness >= 1.0)
            infos.append(info)

        return next_obs, rewards, dones, infos

    def close(self):
        self._executor.shutdown(wait=False)
