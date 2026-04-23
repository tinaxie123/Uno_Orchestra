"""
SkillRouter multi-turn generation manager for verl.

Drop-in replacement for Router-R1's `LLMGenerationManager`.  The only
behaviours that need to change from Router-R1 are (1) how a rollout chunk
is parsed into an action and (2) how routed sub-agent calls are
executed; the tensor plumbing (rolling state, info mask, GPU padding,
output assembly) is unchanged.

The rollout round's grammar is the schema v1.1 the SFT router was
trained on:

    <plan round="N">
      <subtask id="K" depends_on="...">instruction</subtask>
      ...
    </plan>
    <route round="N" subtask="K" model="M" skill="S">query</route>
    <route round="N" subtask="L" model="M" skill="S">query</route>
    <!-- manager injects exactly here, after the closing </route>: -->
    <obs subtask="K">worker-response</obs>
    <obs subtask="L">worker-response</obs>
    <!-- policy resumes -->
    <verify round="N">...</verify>
    <final_answer>...</final_answer>

Each LLM turn must emit either (a) one complete `<plan>`-block with
all its `<route>`s (we stop at the last `</route>` that belongs to the
latest `<plan round="N">`), or (b) a `<final_answer>...</final_answer>`
which terminates the episode.  Everything else is an invalid action and
gets a 0 reward block without any observation injected.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from multiprocessing.dummy import Pool as ThreadPool
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch

from verl import DataProto

# Tensor helpers are unchanged from Router-R1.
from router_r1.llm_agent.tensor_helper import TensorConfig, TensorHelper

# Worker-API call + price table come straight from the skillrouter env.
_ENV_PKG = "/data/xieht/verl-agent"
if _ENV_PKG not in sys.path:
    sys.path.insert(0, _ENV_PKG)
from agent_system.environments.env_package.skillrouter.envs import (  # noqa: E402
    MODEL_COST_PER_M_TOKENS,
    VALID_MODELS,
    VALID_SKILLS,
    call_sub_agent_api,
)


# ──────────────────────────────────────────────────────────────────────
# Parse regexes — aligned with envs.py line-for-line so behaviour here
# matches what the env would do at step time.
# ──────────────────────────────────────────────────────────────────────
PLAN_OPEN_RE = re.compile(r'<plan round="(\d+)"\s*>', re.DOTALL)
PLAN_CLOSE_RE = re.compile(r"</plan\s*>", re.DOTALL)
ROUTE_RE = re.compile(
    r'<route\s+round="(\d+)"\s+subtask="(\d+)"\s+model="([^"]+)"\s+skill="([^"]+)"\s*>'
    r"(.*?)</route\s*>",
    re.DOTALL,
)
FINAL_RE = re.compile(r"<final_answer\s*>(.*?)</final_answer\s*>", re.DOTALL)


@dataclass
class GenerationConfig:
    max_turns: int = 5
    max_start_length: int = 2048
    max_prompt_length: int = 4096
    max_response_length: int = 2048
    max_obs_length: int = 512
    num_gpus: int = 8
    no_think_rl: bool = False
    exp_name: Optional[str] = None
    # Accepted but ignored — Router-R1's trainer passes these through
    # from the top-level config; our worker API lives in envs.py (reads
    # DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL from env vars).  We keep
    # the fields so drop-in substitution doesn't break instantiation.
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    # Concurrency for the worker-API thread pool inside execute_predictions.
    route_concurrency: int = 32
    # Timeout per worker call; the env default is hidden inside the openai
    # client, we surface it here so validation can shrink it.
    route_timeout_s: float = 60.0
    # Hard cap: if a sample blows past this many USD of total cost across
    # its rollout it is forced to done (matches MAX_EPISODE_COST in envs).
    max_episode_cost_usd: float = field(
        default_factory=lambda: MODEL_COST_PER_M_TOKENS["claude-opus-4-6"] * 500 / 1e6 * 8
    )


# ──────────────────────────────────────────────────────────────────────
# Pure-string helpers (no tensors) — easy to unit-test.
# ──────────────────────────────────────────────────────────────────────
def _truncate_at_action_boundary(text: str) -> str:
    """Cut the decoded response at the first action-ending boundary.

    Policy follows the SFT turn-flow order:
      1. Last `</route>` of the latest plan-round  — preferred; the
         manager must inject <obs> right after this and let the model
         resume with the worker results.
      2. `</final_answer>`                         — terminal fallback
         (only hit when the model closes the episode without another
         routing round in this turn).

    Checking route first guarantees that if a single response contains
    both a complete plan+route block and a speculative final_answer,
    we execute the route (so the next turn sees real obs) rather than
    short-circuiting on the model's guess.  If neither is present we
    return the whole string and treat it as an invalid action downstream.
    """
    end = _find_last_route_end_in_latest_plan(text)
    if end > 0:
        return text[:end]

    m_final = FINAL_RE.search(text)
    if m_final:
        return text[: m_final.end()]

    return text


def _find_last_route_end_in_latest_plan(text: str) -> int:
    """Return char-index *after* the last `</route>` tag that belongs to
    the most recently opened `<plan round="N">` block.

    Policy: a plan-block has ended from the manager's POV once we see
    `</plan>` AND at least one `<route>` closed. We inject <obs>s *after*
    the last `</route>` (not after `</plan>`) because the SFT schema
    places routes *outside* the plan element.
    """
    plan_open = None
    for m in PLAN_OPEN_RE.finditer(text):
        plan_open = m
    if plan_open is None:
        return -1

    # Find `</plan>` after this open; bail if not present yet.
    m_close = PLAN_CLOSE_RE.search(text, plan_open.end())
    if m_close is None:
        return -1

    # Find the last `</route>` after the plan close. Routes are expected
    # to follow the closing </plan> in the schema.
    last_route_end = -1
    for m in ROUTE_RE.finditer(text, m_close.end()):
        last_route_end = m.end()
    return last_route_end


def _parse_final_answer(text: str) -> Optional[str]:
    m = FINAL_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_routes_for_latest_round(text: str) -> List[Dict[str, Any]]:
    """Return every `<route round="N" ...>` where N is the *last* plan round.

    A route entry: {"round", "subtask", "model", "skill", "query"}.
    """
    plans = list(PLAN_OPEN_RE.finditer(text))
    if not plans:
        return []
    latest_round = plans[-1].group(1)

    routes = []
    for m in ROUTE_RE.finditer(text, plans[-1].end()):
        rnd, sub, model, skill, query = m.groups()
        if rnd != latest_round:
            continue
        routes.append(
            {
                "round": rnd,
                "subtask": sub,
                "model": model.strip(),
                "skill": skill.strip(),
                "query": query.strip(),
            }
        )
    return routes


def _format_obs_block(routes: List[Dict[str, Any]], responses: List[str]) -> str:
    """Build the `<obs subtask="...">...</obs>` block to splice into the
    rolling sequence, in the order the SFT router was trained to consume.
    """
    parts = ["\n"]
    for r, resp in zip(routes, responses):
        parts.append(f'<obs subtask="{r["subtask"]}">{resp}</obs>\n')
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Generation manager
# ──────────────────────────────────────────────────────────────────────
class SkillRouterGenerationManager:
    """Multi-turn rollout driver with real sub-agent API calls.

    Mirrors Router-R1's `LLMGenerationManager` interface so the same
    ray_trainer.py drives both; the overrides are:
      - `postprocess_predictions` (schema v1.1 instead of <search>/<answer>)
      - `execute_predictions`    (dispatches routes to the worker pool)
    """

    def __init__(
        self,
        tokenizer,
        actor_rollout_wg,
        config: GenerationConfig,
        questions: Optional[List[str]] = None,
        is_validation: bool = False,
    ):
        self.tokenizer = tokenizer
        self.actor_rollout_wg = actor_rollout_wg
        self.config = config
        self.is_validation = is_validation

        # The sub-agent API prompt wants the *original* task question in
        # addition to the per-subtask instruction, so the manager needs
        # per-sample questions.  Injected by set_questions() before
        # each run_llm_loop call.
        self._questions: Optional[List[str]] = questions
        # Per-sample running cost — forced-done once max_episode_cost
        # is crossed.
        self._episode_cost: Optional[np.ndarray] = None

        self.tensor_fn = TensorHelper(
            TensorConfig(
                pad_token_id=tokenizer.pad_token_id,
                max_prompt_length=config.max_prompt_length,
                max_obs_length=config.max_obs_length,
                max_start_length=config.max_start_length,
            )
        )

    # Public hook — call this from ray_trainer.py right before
    # run_llm_loop, once questions have been gathered from the batch.
    def set_questions(self, questions: List[str]) -> None:
        self._questions = list(questions)

    # ── Tensor plumbing (unchanged from Router-R1) ─────────────────
    def _batch_tokenize(self, texts: List[str]) -> torch.Tensor:
        return self.tokenizer(
            texts,
            add_special_tokens=False,
            return_tensors="pt",
            padding="longest",
        )["input_ids"]

    def _postprocess_responses(self, responses: torch.Tensor) -> Tuple[torch.Tensor, List[str]]:
        """Decode and truncate each response to its first action boundary."""
        strs = self.tokenizer.batch_decode(responses, skip_special_tokens=True)
        truncated = [_truncate_at_action_boundary(s) for s in strs]
        ids = self._batch_tokenize(truncated)
        return ids, truncated

    def _process_next_obs(self, next_obs: List[str]) -> torch.Tensor:
        ids = self.tokenizer(
            next_obs,
            padding="longest",
            return_tensors="pt",
            add_special_tokens=False,
        )["input_ids"]
        if ids.shape[1] > self.config.max_obs_length:
            ids = ids[:, : self.config.max_obs_length]
        return ids

    def _update_rolling_state(
        self,
        rollings: DataProto,
        cur_responses: torch.Tensor,
        next_obs_ids: torch.Tensor,
    ) -> DataProto:
        new_input_ids = self.tensor_fn.concatenate_with_padding(
            [rollings.batch["input_ids"], cur_responses, next_obs_ids]
        )
        new_attention_mask = self.tensor_fn.create_attention_mask(new_input_ids)
        new_position_ids = self.tensor_fn.create_position_ids(new_attention_mask)

        effective_len = new_attention_mask.sum(dim=1).max()
        max_len = min(self.config.max_prompt_length, int(effective_len))

        new_rollings = DataProto.from_dict(
            {
                "input_ids": new_input_ids[:, -max_len:],
                "position_ids": new_position_ids[:, -max_len:],
                "attention_mask": new_attention_mask[:, -max_len:],
            }
        )
        new_rollings.meta_info.update(rollings.meta_info)
        return new_rollings

    def _info_masked_concat(
        self,
        prompt: torch.Tensor,
        prompt_mask: torch.Tensor,
        response: torch.Tensor,
        info: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Left-pack concat that also tracks which positions are info (=obs),
        so the loss mask can zero them out later."""
        pad_id = self.tokenizer.pad_token_id
        tensors = [prompt, response]
        tensors_with_mask = [prompt_mask, response]
        if info is not None:
            tensors.append(info)
            tensors_with_mask.append(torch.full_like(info, pad_id))

        concatenated = torch.cat(tensors, dim=1)
        concatenated_with_info = torch.cat(tensors_with_mask, dim=1)
        mask = concatenated != pad_id
        sorted_indices = mask.to(torch.int64).argsort(dim=1, stable=True)
        return (
            concatenated.gather(1, sorted_indices),
            concatenated_with_info.gather(1, sorted_indices),
        )

    def _update_right_side(
        self,
        right_side: Dict[str, torch.Tensor],
        cur_responses: torch.Tensor,
        next_obs_ids: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        responses, responses_mask = self._info_masked_concat(
            right_side["responses"],
            right_side["responses_with_info_mask"],
            cur_responses,
            next_obs_ids,
        )
        effective_len = self.tensor_fn.create_attention_mask(responses).sum(dim=1).max()
        max_len = min(self.config.max_prompt_length, int(effective_len))
        return {
            "responses": responses[:, :max_len],
            "responses_with_info_mask": responses_mask[:, :max_len],
        }

    def _generate_with_gpu_padding(self, active_batch: DataProto) -> DataProto:
        n = self.config.num_gpus
        if n <= 1:
            return self.actor_rollout_wg.generate_sequences(active_batch)

        bs = active_batch.batch["input_ids"].shape[0]
        remainder = bs % n
        for k in active_batch.batch.keys():
            active_batch.batch[k] = active_batch.batch[k].long()
        if remainder == 0:
            return self.actor_rollout_wg.generate_sequences(active_batch)

        pad = n - remainder
        padded = {
            k: torch.cat([v, v[:1].repeat(pad, *[1] * (v.dim() - 1))], dim=0)
            for k, v in active_batch.batch.items()
        }
        padded_batch = DataProto.from_dict(padded)
        for k in padded_batch.batch.keys():
            padded_batch.batch[k] = padded_batch.batch[k].long()
        out = self.actor_rollout_wg.generate_sequences(padded_batch)
        out.batch = {k: v[:-pad] for k, v in out.batch.items()}
        if hasattr(out, "meta_info") and out.meta_info:
            out.meta_info = {
                k: (v[:-pad] if isinstance(v, torch.Tensor) else v)
                for k, v in out.meta_info.items()
            }
        return out

    # ── Core overrides ─────────────────────────────────────────────
    def postprocess_predictions(
        self,
        predictions: List[str],
    ) -> Tuple[List[Optional[str]], List[Any]]:
        """
        Classify each decoded response.

          action:
            "finish"  → the response closes the episode (<final_answer>)
            "route"   → the response completed a plan/route round
            None      → invalid format (no full plan-round, no answer)

          content:
            For "finish": the answer string.
            For "route":  the list of route dicts in the latest round.
            For None:     "".
        """
        actions: List[Optional[str]] = []
        contents: List[Any] = []
        for pred in predictions:
            if not isinstance(pred, str):
                raise ValueError(f"bad prediction type: {type(pred)}")

            # Route first (matches the truncation policy — if both markers
            # are present, prefer the routing action so obs actually fire).
            routes = _parse_routes_for_latest_round(pred)
            if routes:
                actions.append("route")
                contents.append(routes)
                continue

            answer = _parse_final_answer(pred)
            if answer is not None:
                actions.append("finish")
                contents.append(answer)
            else:
                actions.append(None)
                contents.append("")
        return actions, contents

    def execute_predictions(
        self,
        predictions: List[str],
        pad_token: str,
        active_mask: torch.Tensor,
        do_route: bool = True,
    ) -> Tuple[List[str], List[bool], List[int], List[int], List[float]]:
        """
        Parse predictions, dispatch their route calls in a single thread
        pool across the batch, build per-sample <obs> blocks.

        Returns
        -------
        next_obs          : injected observation string per sample
        dones             : True when episode ends (finish / invalid /
                            cost-cap exceeded)
        valid_action      : 1 if action parsed cleanly, else 0
        is_route          : 1 if action was a route (gated by do_route)
        per_sample_cost   : USD cost added by this step
        """
        if self._episode_cost is None:
            self._episode_cost = np.zeros(len(predictions), dtype=np.float32)

        actions, contents = self.postprocess_predictions(predictions)

        # Collect route calls for one batched pool.map — store
        # (sample_idx, route_dict, route_order_within_sample).
        calls: List[Tuple[int, int, Dict[str, Any]]] = []
        # Per-sample bookkeeping
        sample_routes: Dict[int, List[Dict[str, Any]]] = {}

        next_obs: List[str] = [""] * len(predictions)
        dones: List[bool] = [False] * len(predictions)
        valid_action: List[int] = [0] * len(predictions)
        is_route_flags: List[int] = [0] * len(predictions)
        per_sample_cost: List[float] = [0.0] * len(predictions)

        for i, (act, content, active) in enumerate(zip(actions, contents, active_mask.tolist())):
            if not active:
                dones[i] = True
                continue

            if act == "finish":
                valid_action[i] = 1
                dones[i] = True
                # no obs needed after final_answer

            elif act == "route" and do_route:
                routes = [r for r in content if _route_is_well_formed(r)]
                if not routes:
                    # routes present but malformed → treat as invalid
                    dones[i] = False
                    continue
                valid_action[i] = 1
                is_route_flags[i] = 1
                sample_routes[i] = routes
                for j, r in enumerate(routes):
                    calls.append((i, j, r))

            elif act == "route" and not do_route:
                # last-turn extra rollout: we don't actually call the
                # worker pool, just flag the action and finish.
                valid_action[i] = 1
                is_route_flags[i] = 1
                dones[i] = True

            else:
                # Invalid action — policy produced neither a full round
                # nor an answer. End the episode with no obs; the reward
                # manager will score 0.
                dones[i] = True

        # Fire all route calls at once.
        if calls and do_route:
            responses = self._dispatch_routes([(c[0], c[2]) for c in calls])
            # Fold responses back into per-sample obs blocks.
            per_sample_bucket: Dict[int, List[Tuple[int, str, float]]] = {}
            for (sample_i, order_j, route), (resp_text, tokens) in zip(calls, responses):
                price_per_m = MODEL_COST_PER_M_TOKENS.get(route["model"], 10.0)
                call_cost = price_per_m * max(tokens, 1) / 1e6
                per_sample_bucket.setdefault(sample_i, []).append(
                    (order_j, resp_text, call_cost)
                )

            for sample_i, items in per_sample_bucket.items():
                items.sort(key=lambda t: t[0])  # preserve route order
                route_order = sample_routes[sample_i]
                resp_texts = [r[1] for r in items]
                next_obs[sample_i] = _format_obs_block(route_order, resp_texts)

                step_cost = sum(r[2] for r in items)
                per_sample_cost[sample_i] = step_cost
                self._episode_cost[sample_i] += step_cost

                # Cost cap: if the sample has now spent more than the
                # per-episode cap, force-done.  Matches env's MAX_EPISODE_COST.
                if self._episode_cost[sample_i] >= self.config.max_episode_cost_usd:
                    dones[sample_i] = True

        return next_obs, dones, valid_action, is_route_flags, per_sample_cost

    def batch_route(
        self,
        queries: List[str] = None,  # noqa: ARG002  — kept for API compat
    ) -> Tuple[List[str], List[float]]:
        """Router-R1 parity hook.  Not used by execute_predictions in this
        subclass — dispatching goes through `_dispatch_routes` directly
        because each call needs (model, skill, query, question)."""
        raise NotImplementedError(
            "SkillRouter uses structured routes; call execute_predictions instead"
        )

    def _dispatch_routes(
        self,
        sample_routes: List[Tuple[int, Dict[str, Any]]],
    ) -> List[Tuple[str, int]]:
        """Call `call_sub_agent_api` for every (sample_idx, route) in parallel.

        Returns a list, same order as input, of (response_text, tokens).
        The sample_idx is only used to look up the per-sample `question`
        string for the worker prompt; it does not affect scheduling.
        """
        if not sample_routes:
            return []

        questions = self._questions or [""] * (max(s[0] for s in sample_routes) + 1)

        def _one(item):
            order, (sample_idx, r) = item
            q = questions[sample_idx] if sample_idx < len(questions) else ""
            try:
                text, tokens = call_sub_agent_api(r["model"], r["skill"], r["query"], q)
            except Exception as e:  # pool.map would otherwise swallow this
                text, tokens = f"API error: {e!r}"[:200], 0
            return order, text, tokens

        workers = min(len(sample_routes), self.config.route_concurrency)
        with ThreadPool(workers) as pool:
            out = pool.map(_one, list(enumerate(sample_routes)))
        out.sort(key=lambda x: x[0])
        return [(text, tokens) for _, text, tokens in out]

    # ── Main loop (mirrors Router-R1) ──────────────────────────────
    def run_llm_loop(
        self,
        gen_batch: DataProto,
        initial_input_ids: torch.Tensor,
    ) -> DataProto:
        # If the caller didn't set questions explicitly, try to recover them
        # from gen_batch.non_tensor_batch['env_kwargs'] (the launcher's
        # DataProto.pop monkey-patch copies this across). Empty fallback is
        # safe — instructions are expected to be self-contained per the
        # planner prompt's rule 2.
        if self._questions is None:
            env_kw = gen_batch.non_tensor_batch.get("env_kwargs") if gen_batch.non_tensor_batch else None
            if env_kw is not None:
                self.set_questions(
                    [(kw or {}).get("question", "") for kw in env_kw]
                )

        bs = gen_batch.batch["input_ids"].shape[0]
        original_left_side = {
            "input_ids": initial_input_ids[:, -self.config.max_start_length:],
        }
        original_right_side = {
            "responses": initial_input_ids[:, []],
            "responses_with_info_mask": initial_input_ids[:, []],
        }

        self._episode_cost = np.zeros(bs, dtype=np.float32)
        batch_completion_tokens = torch.zeros(bs, dtype=torch.float32)
        active_mask = torch.ones(bs, dtype=torch.bool)
        turns_stats = torch.ones(bs, dtype=torch.int)
        valid_action_stats = torch.zeros(bs, dtype=torch.int)
        valid_route_stats = torch.zeros(bs, dtype=torch.int)
        active_num_list = [active_mask.sum().item()]
        rollings = gen_batch
        meta_info: Dict[str, Any] = {}

        for step in range(self.config.max_turns):
            if active_mask.sum().item() <= 0:
                break

            rollings.batch = self.tensor_fn.cut_to_effective_len(
                rollings.batch,
                keys=["input_ids", "attention_mask", "position_ids"],
            )
            rollings_active = DataProto.from_dict(
                {k: v[active_mask] for k, v in rollings.batch.items()}
            )
            try:
                gen_output = self._generate_with_gpu_padding(rollings_active)
            except Exception as exc:
                print(f"[SkillRouterGen] generate failed at turn {step}: {exc!r}")
                break

            meta_info = gen_output.meta_info or {}
            responses_ids, responses_str = self._postprocess_responses(
                gen_output.batch["responses"]
            )
            responses_ids, responses_str = self.tensor_fn._example_level_pad(
                responses_ids, responses_str, active_mask
            )

            next_obs, dones, valid_action, is_route, step_costs = self.execute_predictions(
                responses_str,
                self.tokenizer.pad_token,
                active_mask,
                do_route=True,
            )

            curr_active_mask = torch.tensor([not d for d in dones], dtype=torch.bool)
            active_mask = active_mask * curr_active_mask
            active_num_list.append(active_mask.sum().item())
            turns_stats[curr_active_mask] += 1
            valid_action_stats += torch.tensor(valid_action, dtype=torch.int)
            valid_route_stats += torch.tensor(is_route, dtype=torch.int)
            batch_completion_tokens += torch.tensor(step_costs, dtype=torch.float32)

            next_obs_ids = self._process_next_obs(next_obs)
            rollings = self._update_rolling_state(rollings, responses_ids, next_obs_ids)
            original_right_side = self._update_right_side(
                original_right_side, responses_ids, next_obs_ids
            )

        # One extra rollout without routing, so samples that were still
        # mid-thought can land on their final_answer.
        if active_mask.sum().item() > 0:
            rollings.batch = self.tensor_fn.cut_to_effective_len(
                rollings.batch,
                keys=["input_ids", "attention_mask", "position_ids"],
            )
            rollings_active = DataProto.from_dict(
                {k: v[active_mask] for k, v in rollings.batch.items()}
            )
            gen_output = self._generate_with_gpu_padding(rollings_active)
            meta_info = gen_output.meta_info or meta_info
            responses_ids, responses_str = self._postprocess_responses(
                gen_output.batch["responses"]
            )
            responses_ids, responses_str = self.tensor_fn._example_level_pad(
                responses_ids, responses_str, active_mask
            )
            _, dones, valid_action, is_route, step_costs = self.execute_predictions(
                responses_str,
                self.tokenizer.pad_token,
                active_mask,
                do_route=False,
            )
            curr_active_mask = torch.tensor([not d for d in dones], dtype=torch.bool)
            active_mask = active_mask * curr_active_mask
            active_num_list.append(active_mask.sum().item())
            valid_action_stats += torch.tensor(valid_action, dtype=torch.int)
            valid_route_stats += torch.tensor(is_route, dtype=torch.int)
            batch_completion_tokens += torch.tensor(step_costs, dtype=torch.float32)
            original_right_side = self._update_right_side(
                original_right_side, responses_ids
            )

        meta_info["turns_stats"] = turns_stats.tolist()
        meta_info["active_mask"] = active_mask.tolist()
        meta_info["valid_action_stats"] = valid_action_stats.tolist()
        meta_info["valid_route_stats"] = valid_route_stats.tolist()
        meta_info["batch_completion_tokens"] = batch_completion_tokens.tolist()
        meta_info["batch_episode_cost_usd"] = self._episode_cost.tolist()
        print(f"[SkillRouterGen] ACTIVE_TRAJ_NUM: {active_num_list}")

        return self._compose_final_output(original_left_side, original_right_side, meta_info)

    def _compose_final_output(
        self,
        left_side: Dict[str, torch.Tensor],
        right_side: Dict[str, torch.Tensor],
        meta_info: Dict[str, Any],
    ) -> DataProto:
        final = dict(right_side)
        final["prompts"] = left_side["input_ids"]
        final["input_ids"] = torch.cat([left_side["input_ids"], right_side["responses"]], dim=1)
        final["attention_mask"] = torch.cat(
            [
                self.tensor_fn.create_attention_mask(left_side["input_ids"]),
                self.tensor_fn.create_attention_mask(final["responses"]),
            ],
            dim=1,
        )
        final["info_mask"] = torch.cat(
            [
                self.tensor_fn.create_attention_mask(left_side["input_ids"]),
                self.tensor_fn.create_attention_mask(final["responses_with_info_mask"]),
            ],
            dim=1,
        )
        final["position_ids"] = self.tensor_fn.create_position_ids(final["attention_mask"])
        out = DataProto.from_dict(final)
        out.meta_info.update(meta_info)
        return out


# Well-formed route: model / skill must be in the registered pools; query
# must be non-empty.  Keeps garbage hallucinations out of the worker API.
def _route_is_well_formed(route: Dict[str, Any]) -> bool:
    if route["model"] not in VALID_MODELS:
        return False
    if route["skill"] not in VALID_SKILLS:
        return False
    if not route["query"]:
        return False
    return True
