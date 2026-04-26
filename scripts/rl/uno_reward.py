"""Uno reward manager — places the env-composed terminal reward.

Companion to ``scripts/rl/uno_rollout.py``. The agent loop already
composes the per-trajectory scalar

    R = (1-α)·R_outcome + α·R_cost          (only when correct, else 0)

and surfaces it in ``AgentLoopOutput.extra_fields["env_terminal_reward"]``.
After verl's ``_postprocess`` flattens those extra fields by key, the
scalar lives at ``data.non_tensor_batch["env_terminal_reward"]`` as an
object array of length batch.

This reward manager is therefore *trivial*: no per-source verifier
re-run, no compute_score callback. It just lays the scalar onto the
last index where ``response_mask == 1`` — i.e. the last *policy*
token of the trajectory — matching the convention that downstream
GRPO advantage computation expects (one reward per response, placed
at the EOS-equivalent of the policy's own emission).

Why ``response_mask`` and not ``valid_response_length - 1``:
the Uno rollout is multi-turn with interleaved observation tokens
(``response_mask`` is 1/0/1/0/...). The "last valid response token"
(``naive``'s convention) would land in an obs span for any rollout
that ends mid-route, which would silently zero out the gradient on
the policy head we actually want to train. Using
``torch.nonzero(response_mask, as_tuple=False)[-1]`` finds the last
policy token regardless of trailing obs.

Side-effect import: importing this module registers ``UnoRewardManager``
under the name ``"uno"``. The launcher selects it via Hydra:
    reward_model.reward_manager.name=uno
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import torch

from verl import DataProto
from verl.workers.reward_manager.abstract import AbstractRewardManager


# Keys the agent loop attaches to every rollout. We surface them as
# `reward_extra_info` so downstream metrics get per-rollout cost,
# correctness, format-validity, etc. without a second pass.
_PASSTHROUGH_KEYS = (
    "env_correctness",
    "env_api_cost",
    "env_n_route_calls",
    "env_n_obs_tokens",
    "env_num_turns",
    "env_format_valid",
    "done_reason",
    "source",
)


class UnoRewardManager(AbstractRewardManager):
    """Place the env-composed terminal reward on the last policy token."""

    def __init__(
        self,
        tokenizer,
        num_examine: int = 0,
        compute_score=None,  # unused — env owns scoring
        reward_fn_key: str = "data_source",
        **kwargs: Any,
    ) -> None:
        self.tokenizer = tokenizer
        self.num_examine = num_examine
        self.reward_fn_key = reward_fn_key

    def __call__(
        self, data: DataProto, return_dict: bool = False
    ) -> torch.Tensor | dict[str, Any]:
        # If a reward loop already wrote rm_scores during rollout, reuse them.
        cached = self._extract_reward_from_rm_scores(data, return_dict)
        if cached is not None:
            return cached

        response_mask = data.batch["response_mask"]  # [bsz, response_length]
        bsz = response_mask.size(0)
        reward_tensor = torch.zeros_like(response_mask, dtype=torch.float32)
        reward_extra_info: dict[str, list] = defaultdict(list)

        terminals = data.non_tensor_batch.get("env_terminal_reward")
        if terminals is None:
            raise KeyError(
                "UnoRewardManager expected non_tensor_batch['env_terminal_reward'] "
                "(emitted by UnoAgentLoop in extra_fields). Did you forget to "
                "import scripts.rl.uno_rollout in the trainer entry?"
            )

        printed: dict[str, int] = {}
        for i in range(bsz):
            row_mask = response_mask[i]
            policy_idx = torch.nonzero(row_mask, as_tuple=False)
            if policy_idx.numel() == 0:
                # No policy tokens at all (e.g. immediate empty generation).
                # Reward stays zero; nothing to place.
                last_pos = -1
            else:
                last_pos = int(policy_idx[-1].item())

            scalar = float(terminals[i])
            if last_pos >= 0:
                reward_tensor[i, last_pos] = scalar

            # Surface per-rollout diagnostics for the metric/tracker logger.
            for key in _PASSTHROUGH_KEYS:
                arr = data.non_tensor_batch.get(key)
                reward_extra_info[key].append(
                    arr[i] if arr is not None else None
                )
            reward_extra_info["env_terminal_reward"].append(scalar)

            data_source = data.non_tensor_batch[self.reward_fn_key][i] \
                if self.reward_fn_key in data.non_tensor_batch else "unknown"
            if self.num_examine and printed.get(data_source, 0) < self.num_examine:
                printed[data_source] = printed.get(data_source, 0) + 1
                # Keep the print compact — full traces are huge.
                print(
                    f"[uno-reward] source={data_source} reward={scalar:.4f} "
                    f"correctness={reward_extra_info['env_correctness'][-1]} "
                    f"cost={reward_extra_info['env_api_cost'][-1]} "
                    f"done={reward_extra_info['done_reason'][-1]}"
                )

        if return_dict:
            return {
                "reward_tensor": reward_tensor,
                "reward_extra_info": {k: np.asarray(v, dtype=object) for k, v in reward_extra_info.items()},
            }
        return reward_tensor
