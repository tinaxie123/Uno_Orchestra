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

This module also exposes ``compute_uno_metrics()``: an aggregator
called from verl's ray_trainer that turns the per-row diagnostics in
``reward_extra_infos_dict`` into scalar wandb metrics under the
``uno/`` namespace (route count, lazy ratio, API spend, done-reason
histogram). Without this, those fields exist in non_tensor_batch but
are never surfaced to the metric stream — the run is reward-only and
opaque to "is the model routing or going lazy".

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

import logging
from collections import defaultdict
from typing import Any

import numpy as np
import torch

from verl import DataProto
from verl.workers.reward_manager.abstract import AbstractRewardManager

logger = logging.getLogger(__name__)


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
            logger.warning(
                "UnoRewardManager: non_tensor_batch['env_terminal_reward'] "
                "missing — falling back to zero rewards for this batch (bsz=%d). "
                "This is expected for non-uno bisect runs; if you see this "
                "with default_agent_loop=uno, check that scripts.rl.uno_rollout "
                "is importable and AgentLoopOutput.extra_fields['env_terminal_reward'] "
                "is being populated.",
                bsz,
            )
            terminals = np.zeros(bsz, dtype=object)

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


# Done-reason buckets we explicitly track. Anything not listed lands in
# `uno/done_reason/other_ratio` so we never silently lose a category.
_DONE_REASONS = (
    "final",
    "lazy",
    "format_error",
    "timeout",
    "response_length",
    "empty_generation",
    "max_turns",
    "done",
)


def compute_uno_metrics(reward_extra_infos_dict: dict[str, list]) -> dict[str, float]:
    """Aggregate UnoAgentLoop per-rollout diagnostics into scalar metrics.

    Called from verl.trainer.ppo.ray_trainer right after
    ``reward_extra_infos_dict`` is materialised. Returns a flat dict keyed
    under the ``uno/`` namespace, ready to ``metrics.update(...)``.

    Quietly returns {} when none of the uno keys are present — that's the
    expected state for non-uno runs (Search-R1, ToolAgentLoop, etc.) so
    this aggregator is safe to call unconditionally.
    """
    if not reward_extra_infos_dict:
        return {}

    def _get(key: str) -> list | None:
        v = reward_extra_infos_dict.get(key)
        if v is None or len(v) == 0:
            return None
        return list(v)

    out: dict[str, float] = {}

    n_routes = _get("env_n_route_calls")
    if n_routes is not None:
        arr = np.asarray([int(x) for x in n_routes], dtype=np.int64)
        out["uno/n_routes/mean"] = float(arr.mean())
        out["uno/n_routes/max"] = float(arr.max())
        out["uno/n_routes/min"] = float(arr.min())
        out["uno/n_routes/sum"] = float(arr.sum())
        # Fraction of rollouts that issued at least one route — direct
        # complement of the lazy ratio for cross-checking.
        out["uno/route_ratio"] = float((arr > 0).mean())

    cost = _get("env_api_cost")
    if cost is not None:
        arr = np.asarray([float(x) for x in cost], dtype=np.float64)
        out["uno/api_cost_usd/mean"] = float(arr.mean())
        out["uno/api_cost_usd/max"] = float(arr.max())
        out["uno/api_cost_usd/sum"] = float(arr.sum())

    correctness = _get("env_correctness")
    if correctness is not None:
        arr = np.asarray([float(x) for x in correctness], dtype=np.float64)
        out["uno/correctness/mean"] = float(arr.mean())

    fmt_valid = _get("env_format_valid")
    if fmt_valid is not None:
        arr = np.asarray([bool(x) for x in fmt_valid], dtype=bool)
        out["uno/format_valid_ratio"] = float(arr.mean())

    num_turns = _get("env_num_turns")
    if num_turns is not None:
        arr = np.asarray([int(x) for x in num_turns], dtype=np.int64)
        out["uno/num_turns/mean"] = float(arr.mean())
        out["uno/num_turns/max"] = float(arr.max())

    n_obs_tok = _get("env_n_obs_tokens")
    if n_obs_tok is not None:
        arr = np.asarray([int(x) for x in n_obs_tok], dtype=np.int64)
        out["uno/n_obs_tokens/mean"] = float(arr.mean())
        out["uno/n_obs_tokens/sum"] = float(arr.sum())

    term_reward = _get("env_terminal_reward")
    if term_reward is not None:
        arr = np.asarray([float(x) for x in term_reward], dtype=np.float64)
        out["uno/terminal_reward/mean"] = float(arr.mean())

    done = _get("done_reason")
    if done is not None:
        labels = [str(x) if x is not None else "unknown" for x in done]
        n = len(labels)
        seen = set()
        for r in _DONE_REASONS:
            cnt = sum(1 for x in labels if x == r)
            out[f"uno/done_reason/{r}_ratio"] = float(cnt) / float(n)
            seen.add(r)
        other = sum(1 for x in labels if x not in seen)
        out["uno/done_reason/other_ratio"] = float(other) / float(n)
        # Convenience aliases for the two most paper-relevant categories.
        out["uno/lazy_ratio"] = out["uno/done_reason/lazy_ratio"]
        out["uno/format_error_ratio"] = out["uno/done_reason/format_error_ratio"]

    return out
