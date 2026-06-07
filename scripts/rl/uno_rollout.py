"""UNO agent loop — schema v1.1 multi-turn router rollout.

Minimum-viable AgentLoopBase subclass for upstream verl v0.7. Registers
under `@register("uno")` so a side-effect import
(`import scripts.rl.uno_rollout`) is enough for the trainer's
agent-loop registry to pick it up.

Per-episode loop (schema v1.1):
    1. policy emits `<plan round=N>` + one or more
       `<route ...>...</route>` blocks  (response_mask = 1)
    2. env.step dispatches each route to the real worker LLM via
       xiaojingai and replies with `<obs subtask=K>...</obs>` blocks,
       which we inject as a `user` turn                 (response_mask = 0)
    3. policy reads obs, emits `<verify>` and either
         (a) another `<plan ...>` — loop to step 1, OR
         (b) `<final_answer>...</final_answer>`         — terminal

Terminal-reward position convention (contract with the reward manager):
    the env composes R = (1-α)·R_outcome + α·R_cost on the step that
    flips `done=True` and we surface that scalar in
    `AgentLoopOutput.extra_fields["env_terminal_reward"]`. The reward
    manager (UnoRewardManager, follow-up commit) writes this
    scalar onto the **last token index where response_mask == 1** —
    i.e. the last policy-generated token of the trajectory. This is
    the same convention verl.trainer uses for other outcome-only RMs.

Known v1 simplifications (to be revisited in follow-up commits):
- α is hard-coded at 0.1 (matches UnoMultiProcessEnv default).
- Observations are wrapped as a `user` chat turn + re-applied template
  (matches upstream ToolAgentLoop). Whether the SFT fixtures used the
  exact same framing is the subject of the byte-identity test.
- No logprobs or multimodal signal.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any
from uuid import uuid4

from transformers import AutoProcessor, AutoTokenizer

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase,
    AgentLoopOutput,
    AsyncLLMServerManager,
    DictConfigWrap,
    register,
)
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op

from env.env_package.uno.envs import (
    SingleUnoEnv,
    _rolling_percentile_cost_reward,
)

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

# Default α for the (1-α)·outcome + α·cost blend. Matches
# UnoMultiProcessEnv's default so the composed terminal reward
# is continuous with runs produced under the old rollout. Overridable
# from Hydra via `actor_rollout_ref.rollout.multi_turn.alpha=<float>`.
_DEFAULT_ALPHA = 0.1


@register("uno")
class UnoAgentLoop(AgentLoopBase):
    """Schema v1.1 router agent loop."""

    def __init__(
        self,
        trainer_config: DictConfigWrap,
        server_manager: AsyncLLMServerManager,
        tokenizer: AutoTokenizer,
        processor: AutoProcessor,
        **kwargs,
    ):
        super().__init__(trainer_config, server_manager, tokenizer, processor, **kwargs)
        config = trainer_config.config
        multi_turn = config.actor_rollout_ref.rollout.multi_turn
        self.max_turns = int(
            getattr(multi_turn, "max_assistant_turns", None)
            or multi_turn.get("max_turns", 5)
        )
        self.prompt_length = int(config.actor_rollout_ref.rollout.prompt_length)
        self.response_length = int(config.actor_rollout_ref.rollout.response_length)
        # Outcome/cost blend weight; see _DEFAULT_ALPHA for rationale.
        self.alpha = float(multi_turn.get("alpha", _DEFAULT_ALPHA))
        self.agentic_shaping_eta = float(
            multi_turn.get("agentic_shaping_eta", multi_turn.get("shaping_eta", 0.05))
        )
        self.agentic_shaping_mode = str(
            multi_turn.get("agentic_shaping_mode", multi_turn.get("shaping_mode", "process"))
        )

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        # Diagnostic for the v3-v8 hang: AgentLoopBase.__init__ captures
        # `self.loop = get_event_loop()` in the Ray actor's sync init context,
        # which may not be the same loop that `run()` actually executes on.
        # If the IDs differ, every `await self.loop.run_in_executor(...)` would
        # silently submit to a non-running loop and deadlock. Always use the
        # running loop instead (this is what upstream ToolAgentLoop does).
        loop = asyncio.get_running_loop()
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "uno run() loop ids: running=%s self.loop=%s match=%s",
                id(loop), id(self.loop), id(loop) == id(self.loop),
            )
        messages = list(kwargs["raw_prompt"])
        extra_info = dict(kwargs.get("extra_info") or {})
        env_kwargs = dict(extra_info.get("env_kwargs") or {})
        for key in ("question", "ground_truth", "data_source", "source", "tests"):
            if key not in env_kwargs and key in extra_info:
                env_kwargs[key] = extra_info[key]
        rm = kwargs.get("reward_model") or {}
        if rm.get("ground_truth") is not None:
            env_kwargs["ground_truth"] = rm["ground_truth"]
        env_kwargs.setdefault("data_source", kwargs.get("data_source", "unknown"))
        env_kwargs.setdefault(
            "source", env_kwargs.get("source") or env_kwargs["data_source"]
        )
        env_kwargs.setdefault("max_turns", self.max_turns)
        env = SingleUnoEnv()
        env.reset(env_kwargs)
        prompt_ids = await self.apply_chat_template(messages)
        request_id = uuid4().hex
        metrics: dict[str, Any] = {}
        response_mask: list[int] = []
        full_ids: list[int] = list(prompt_ids)

        num_turns = 0
        n_route_calls = 0
        n_obs_tokens = 0
        done_reason = "max_turns"
        env_terminal_reward = 0.0
        env_meta_last: dict[str, Any] = {}
        turn_starts: list[int] = []
        turn_ends: list[int] = []
        turn_indices: list[int] = []
        turn_action_types: list[str] = []
        turn_shaping_rewards: list[float] = []
        turn_parent_prefix_hashes: list[str] = []

        for _turn in range(self.max_turns):
            parent_prefix_hash = _hash_token_prefix(full_ids)
            with simple_timer("generate_sequences", metrics):
                gen = await self.server_manager.generate(
                    request_id=request_id,
                    prompt_ids=full_ids,
                    sampling_params=sampling_params,
                )
            turn_ids = list(gen.token_ids)
            if not turn_ids:
                done_reason = "empty_generation"
                break

            turn_start = len(response_mask)
            full_ids.extend(turn_ids)
            response_mask.extend([1] * len(turn_ids))
            num_turns += 1
            turn_end = min(len(response_mask), self.response_length)
            if len(response_mask) >= self.response_length:
                done_reason = "response_length"
                _append_turn_credit_metadata(
                    turn_starts,
                    turn_ends,
                    turn_indices,
                    turn_action_types,
                    turn_shaping_rewards,
                    turn_parent_prefix_hashes,
                    start=turn_start,
                    end=turn_end,
                    turn_index=num_turns,
                    action_type="response_length",
                    shaping_reward=0.0,
                    parent_prefix_hash=parent_prefix_hash,
                )
                break
            turn_text = await loop.run_in_executor(
                None,
                lambda ids=turn_ids: self.tokenizer.decode(
                    ids, skip_special_tokens=True
                ),
            )
            # env.step() dispatches route primitives through the UNO harness.
            # Running it inline would block the
            # AgentLoopWorker's event loop, stalling all other rollouts on
            # the same worker — that's the v3-v6b smoke hang.
            # Default-arg lambda binds turn_text at definition time, since
            # the variable is reassigned each iteration of the outer loop.
            step = await loop.run_in_executor(
                None,
                lambda t=turn_text: env.step(t),
            )
            env_meta_last = step.get("metadata", {}) or {}
            n_route_calls += int(env_meta_last.get("n_routes", 0))
            action_type = _classify_turn_action(env_meta_last, step_done=bool(step["done"]))
            shaping_reward = _compute_turn_shaping_reward(
                env_meta_last,
                action_type=action_type,
                eta=self.agentic_shaping_eta,
                mode=self.agentic_shaping_mode,
            )
            _append_turn_credit_metadata(
                turn_starts,
                turn_ends,
                turn_indices,
                turn_action_types,
                turn_shaping_rewards,
                turn_parent_prefix_hashes,
                start=turn_start,
                end=turn_end,
                turn_index=num_turns,
                action_type=action_type,
                shaping_reward=shaping_reward,
                parent_prefix_hash=parent_prefix_hash,
            )

            if step["done"]:
                if env_meta_last.get("final_answer") is not None:
                    done_reason = "lazy" if env_meta_last.get("lazy_mode") else "final"
                elif env_meta_last.get("format_error"):
                    done_reason = "format_error"
                elif env_meta_last.get("timeout"):
                    done_reason = "timeout"
                else:
                    done_reason = "done"
                env_terminal_reward = _compose_terminal_reward(env, step, self.alpha)
                break
            obs_list = step.get("observations") or []
            if not obs_list:
                continue
            obs_content = obs_list[0].get("content", "") or ""
            if not obs_content:
                continue

            # SFT was done with LlamaFactory's qwen template, whose
            # `format_observation` emits:
            #     <|im_start|>user\n<tool_response>\n{content}\n</tool_response>
            #         <|im_end|>\n<|im_start|>assistant\n
            # The bundled Qwen2.5 chat_template reproduces this exactly when
            # rendered with role="tool" (it auto-wraps tool messages in the
            # <tool_response> envelope under role=user). Using role="user"
            # would emit a bare `<|im_start|>user\n{content}…` block — one+
            # tokens off from SFT, which is what drove turn-2 format_error
            # under the v3-v8 stack. apply_chat_template + remove_system_prompt
            # is the same path upstream ToolAgentLoop uses.
            obs_messages = [{"role": "tool", "content": obs_content}]
            obs_ids = await self.apply_chat_template(
                obs_messages, remove_system_prompt=True
            )
            # Inter-turn newline guard. vLLM stops at <|im_end|> and does
            # NOT emit the trailing '\n' that LlamaFactory's
            # format_assistant ({{content}}<|im_end|>\n) wrote into the SFT
            # corpus. Without this guard the obs splice produces
            # ...<|im_end|><|im_start|>user\n... — one byte off from SFT
            # at every turn boundary, observed in canary v10 byte-sanity
            # log as the residual driver of turn-2 format_error. Insert a
            # single '\n' (Qwen2 tokenizer: id 198) before the obs block
            # iff the previous token isn't already '\n'. Mask it as a
            # non-policy token (0) since vLLM didn't generate it.
            newline_ids = self.tokenizer(
                "\n", add_special_tokens=False
            ).input_ids
            if newline_ids and (
                not full_ids or full_ids[-1] != newline_ids[-1]
            ):
                full_ids.extend(newline_ids)
                response_mask.extend([0] * len(newline_ids))
            full_ids.extend(obs_ids)
            response_mask.extend([0] * len(obs_ids))
            n_obs_tokens += len(obs_ids)
            # One-shot byte-sanity log per agent-loop actor — decode the
            # last 200 tokens after the first obs splice and emit at INFO,
            # so a smoke run yields visual evidence that what the model
            # sees at turn 2 matches the LlamaFactory SFT framing.
            if (
                not getattr(UnoAgentLoop, "_byte_sanity_logged", False)
                and logger.isEnabledFor(logging.INFO)
            ):
                tail_ids = full_ids[-200:]
                tail_text = await loop.run_in_executor(
                    None,
                    lambda ids=tail_ids: self.tokenizer.decode(
                        ids, skip_special_tokens=False
                    ),
                )
                logger.info(
                    "uno byte-sanity (first obs splice, last 200 tok): %r",
                    tail_text,
                )
                UnoAgentLoop._byte_sanity_logged = True

            if len(response_mask) >= self.response_length:
                done_reason = "response_length"
                break
        if not env.done:
            env_terminal_reward = 0.0
            env_meta_last.setdefault("timeout", True)
            env_meta_last.setdefault("correctness", 0.0)
        resp_len = min(len(response_mask), self.response_length)
        prompt_ids_out = full_ids[: len(full_ids) - len(response_mask)]
        response_ids_out = full_ids[-len(response_mask):][:resp_len] if response_mask else []
        response_mask_out = response_mask[:resp_len]
        extra_fields: dict[str, Any] = {
            "env_terminal_reward": float(env_terminal_reward),
            "env_correctness": float(env_meta_last.get("correctness", 0.0) or 0.0),
            "env_api_cost": float(env.total_api_cost),
            "env_n_route_calls": int(n_route_calls),
            "env_n_obs_tokens": int(n_obs_tokens),
            "env_num_turns": int(num_turns),
            "env_format_valid": bool(env_meta_last.get("format_valid", True)),
            "done_reason": done_reason,
            "data_source": env.data_source,
            "source": env.source,
            "agentic_turn_start": turn_starts,
            "agentic_turn_end": turn_ends,
            "agentic_turn_index": turn_indices,
            "agentic_action_type": turn_action_types,
            "agentic_turn_shaping_reward": turn_shaping_rewards,
            "agentic_parent_prefix_hash": turn_parent_prefix_hashes,
        }
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "uno rollout: turns=%d routes=%d obs_tok=%d "
                "done=%s reward=%.4f cost=%.4g source=%s",
                num_turns, n_route_calls, n_obs_tokens,
                done_reason, env_terminal_reward, env.total_api_cost, env.source,
            )

        return AgentLoopOutput(
            prompt_ids=prompt_ids_out,
            response_ids=response_ids_out,
            response_mask=response_mask_out,
            num_turns=num_turns,
            metrics=metrics,
            extra_fields=extra_fields,
        )


def _compose_terminal_reward(
    env: SingleUnoEnv, step: dict, alpha: float
) -> float:
    """Reproduce UnoMultiProcessEnv's terminal reward rule for one env.

        mid-step                 → 0.0
        terminal, answer wrong   → 0.0   (includes malformed output)
        terminal, answer correct → (1-α)·1 + α·(1 - cost/rolling-hi)

    Kept here because the agent loop runs per-sample without the vector
    env wrapper that owns that blending in the old stack.
    """
    correctness = float(step.get("reward", 0.0) or 0.0)
    if correctness <= 0:
        return 0.0
    r_cost = _rolling_percentile_cost_reward(env.total_api_cost)
    return (1.0 - alpha) * correctness + alpha * r_cost


def _append_turn_credit_metadata(
    starts: list[int],
    ends: list[int],
    indices: list[int],
    action_types: list[str],
    shaping_rewards: list[float],
    parent_prefix_hashes: list[str],
    *,
    start: int,
    end: int,
    turn_index: int,
    action_type: str,
    shaping_reward: float,
    parent_prefix_hash: str,
) -> None:
    if end <= start:
        return
    starts.append(int(start))
    ends.append(int(end))
    indices.append(int(turn_index))
    action_types.append(str(action_type))
    shaping_rewards.append(float(shaping_reward))
    parent_prefix_hashes.append(str(parent_prefix_hash))


def _hash_token_prefix(token_ids: list[int]) -> str:
    """Stable id for the parent conversation state before a branch action."""
    h = hashlib.blake2b(digest_size=16)
    for token_id in token_ids:
        h.update(int(token_id).to_bytes(8, byteorder="little", signed=True))
    return h.hexdigest()


def _classify_turn_action(metadata: dict[str, Any], *, step_done: bool) -> str:
    if metadata.get("format_error"):
        return "format_error"
    if metadata.get("timeout"):
        return "timeout"
    if metadata.get("final_answer") is not None:
        return "lazy" if metadata.get("lazy_mode") else "final"
    if int(metadata.get("n_routes", 0) or 0) > 0:
        return "repair" if int(metadata.get("round", 1) or 1) > 1 else "route"
    return "done" if step_done else "other"


def _compute_turn_shaping_reward(
    metadata: dict[str, Any], *, action_type: str, eta: float, mode: str = "process"
) -> float:
    eta = max(float(eta), 0.0)
    if eta == 0:
        return 0.0
    mode = (mode or "process").strip().lower()
    if mode in {"none", "off", "false", "0"}:
        return 0.0
    reward = eta if bool(metadata.get("format_valid", True)) else -eta
    if action_type == "format_error":
        reward = -eta
    elif mode == "process" and action_type == "repair":
        reward += 0.5 * eta
    return max(-eta, min(eta, reward))
