import os
from typing import List, Tuple, Dict, Any
import numpy as np
from agent_system.environments.base import EnvironmentManagerBase, to_numpy
_SYSTEM_PROMPT_PATH = os.environ.get(
    "SKILLROUTER_SYSTEM_PROMPT",
    "/home/xieht/data/sft/system_prompt.txt"
)
try:
    with open(_SYSTEM_PROMPT_PATH) as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    SYSTEM_PROMPT = ""
    print(f"WARNING: System prompt not found at {_SYSTEM_PROMPT_PATH}")


INIT_TEMPLATE = """{system_prompt}

Question: {question}

Output the trajectory now."""

STEP_TEMPLATE = """{system_prompt}

Question: {question}

Prior observations from sub-agents:
{history}

Continue the trajectory. Generate <verify> and either <final_answer> or a repair <plan>."""


class SkillRouterEnvironmentManager(EnvironmentManagerBase):
  

    def __init__(self, envs, projection_f, config):
        super().__init__(envs, projection_f, config)
        self.questions = []
        self.history = []  # list of lists, one per env

    def reset(self, kwargs) -> Tuple[Dict[str, Any], List[Dict]]:
        obs, infos = self.envs.reset(kwargs=kwargs)
        self.questions = obs
        self.history = [[] for _ in range(len(obs))]

        observations = {
            "text": self._build_init_obs(obs),
            "image": None,
            "anchor": obs.copy(),
        }
        return observations, infos

    def step(self, text_actions: List[str]):
        actions, valids = self.projection_f(text_actions)
        next_obs, rewards, dones, infos = self.envs.step(actions)

        # Store history
        for i in range(len(next_obs)):
            if i < len(self.history):
                self.history[i].append(next_obs[i])

        anchor = [obs if obs else "" for obs in next_obs]

        next_observations = {
            "text": self._build_step_obs(next_obs),
            "image": None,
            "anchor": anchor,
        }

        for i, info in enumerate(infos):
            info["is_action_valid"] = to_numpy(valids[i])

        rewards = to_numpy(rewards)
        dones = to_numpy(dones)
        return next_observations, rewards, dones, infos

    def _build_init_obs(self, questions: List[str]) -> List[str]:
        """Build initial observation with full system prompt + question."""
        result = []
        for q in questions:
            text = INIT_TEMPLATE.format(
                system_prompt=SYSTEM_PROMPT,
                question=q,
            )
            result.append(text)
        return result

    def _build_step_obs(self, obs_list: List[str]) -> List[str]:
        """Build observation with system prompt + question + history."""
        result = []
        for i, obs in enumerate(obs_list):
            history_parts = self.history[i] if i < len(self.history) else []
            history = "\n".join(h for h in history_parts if h) if history_parts else obs

            text = STEP_TEMPLATE.format(
                system_prompt=SYSTEM_PROMPT,
                question=self.questions[i] if i < len(self.questions) else "",
                history=history,
            )
            result.append(text)
        return result

    def _process_batch(self, batch_idx, total_batch_list, total_infos, success):
        for i in reversed(range(len(total_batch_list[batch_idx]))):
            batch_item = total_batch_list[batch_idx][i]
            if batch_item["active_masks"]:
                info = total_infos[batch_idx][i]
                won_value = float(info.get("won", 0))
                success["success_rate"].append(won_value)

                data_source = info.get("data_source", "unknown")
                success[f"{data_source}_success_rate"].append(won_value)
                return
