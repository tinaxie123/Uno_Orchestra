"""
SkillRouter Environment Manager for verl-agent.
Wraps SkillRouterMultiProcessEnv with the verl-agent interface.
"""

from typing import List, Tuple, Dict, Any
import numpy as np
from agent_system.environments.base import EnvironmentManagerBase, to_numpy


class SkillRouterEnvironmentManager(EnvironmentManagerBase):
    """
    EnvironmentManager for SkillRouter.

    Observation flow:
    - reset(): returns question as initial observation
    - step(): model generates <plan>+<route>, env returns <obs> tags
    - anchor: the raw <obs> text (for GiGPO step-level grouping)
    """

    def __init__(self, envs, projection_f, config):
        super().__init__(envs, projection_f, config)
        self.questions = []

    def reset(self, kwargs) -> Tuple[Dict[str, Any], List[Dict]]:
        obs, infos = self.envs.reset(kwargs=kwargs)
        self.questions = obs  # Store questions for text obs building

        observations = {
            "text": self._build_init_obs(obs),
            "image": None,
            "anchor": obs.copy(),  # Initial anchor = question
        }
        return observations, infos

    def step(self, text_actions: List[str]):
        actions, valids = self.projection_f(text_actions)
        next_obs, rewards, dones, infos = self.envs.step(actions)

        # Anchor for GiGPO: the <obs> content returned by sub-agents.
        # This groups trajectories that reached the same state.
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
        """Build initial observation (just the question)."""
        # The system prompt is in the prompt, so we just need the question
        # as the environment observation. The rollout loop handles chat template.
        return [f"Question: {q}\n\nOutput the trajectory now." for q in questions]

    def _build_step_obs(self, obs_list: List[str]) -> List[str]:
        """Build observation after environment step (the <obs> tags)."""
        result = []
        for obs in obs_list:
            if obs:
                result.append(obs)
            else:
                result.append("")
        return result

    def _process_batch(self, batch_idx, total_batch_list, total_infos, success):
        """Process a single batch for success evaluation."""
        for i in reversed(range(len(total_batch_list[batch_idx]))):
            batch_item = total_batch_list[batch_idx][i]
            if batch_item["active_masks"]:
                info = total_infos[batch_idx][i]
                won_value = float(info.get("won", 0))
                success["success_rate"].append(won_value)

                data_source = info.get("data_source", "unknown")
                success[f"{data_source}_success_rate"].append(won_value)
                return
