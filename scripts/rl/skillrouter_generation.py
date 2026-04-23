"""
SkillRouter multi-turn generation manager for verl GRPO training.

Replaces Router-R1's LLMGenerationManager with SkillRouter's
plan_subtask/finish tool-call format and real sub-agent API calls.

Multi-turn loop:
  1. Router generates: plan_subtask(instruction, model, skill) or finish(answer)
  2. If plan_subtask → call real sub-agent API → return observation
  3. If finish → done, compute correctness reward
  4. Repeat up to max_turns

Compatible with verl's ray_trainer.py (drop-in replacement for
router_r1.llm_agent.generation.LLMGenerationManager).
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import torch
import numpy as np
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from multiprocessing.dummy import Pool as ThreadPool

from verl import DataProto

# Import SkillRouter env for real API calls
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from agent_system.environments.env_package.skillrouter.envs import (
    call_sub_agent_api, MODEL_COST_PER_M_TOKENS,
)

# Import tensor helper from Router-R1
from router_r1.llm_agent.tensor_helper import TensorHelper, TensorConfig


@dataclass
class GenerationConfig:
    max_turns: int = 3
    max_start_length: int = 256
    max_prompt_length: int = 4096
    max_response_length: int = 2048
    max_obs_length: int = 512
    num_gpus: int = 4
    no_think_rl: bool = False
    exp_name: str = None
    api_base: str = None
    api_key: str = None


# ── Action parsing regexes (SkillRouter format) ─────────────
# plan_subtask: {"name":"plan_subtask","arguments":{"instruction":"...","task_id":"t1"}}
# finish: {"name":"finish","arguments":{"answer":"..."}}
# Also support: <plan>...</plan><route>...</route> and <final_answer>...</final_answer>

FINISH_RE = re.compile(
    r'"name"\s*:\s*"finish".*?"arguments"\s*:\s*\{.*?"answer"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)
PLAN_SUBTASK_RE = re.compile(
    r'"name"\s*:\s*"plan_subtask".*?"arguments"\s*:\s*\{(.*?)\}',
    re.DOTALL,
)
FINAL_ANSWER_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

# Also support Router-R1 style <search>Model:query</search>
SEARCH_RE = re.compile(r'<search>\s*([^:<>\n]+?)\s*:\s*(.*?)</search>', re.DOTALL)
ANSWER_RE = re.compile(r'<answer>(.*?)</answer>', re.DOTALL)


def _parse_action(text: str) -> Tuple[str, str]:
    """Parse router output into (action_type, content).

    Returns:
        ("finish", answer_text) — episode done
        ("route", json_with_model_skill_query) — need API call
        ("invalid", "") — bad format
    """
    # Check finish first
    m = FINISH_RE.search(text)
    if m:
        return "finish", m.group(1)

    m = FINAL_ANSWER_RE.search(text)
    if m:
        return "finish", m.group(1).strip()

    m = ANSWER_RE.search(text)
    if m:
        return "finish", m.group(1).strip()

    # Check plan_subtask
    m = PLAN_SUBTASK_RE.search(text)
    if m:
        args_str = m.group(1)
        return "route", args_str

    # Check Router-R1 style <search>
    m = SEARCH_RE.search(text)
    if m:
        model_name = m.group(1).strip()
        query = m.group(2).strip()
        return "route", json.dumps({"model": model_name, "query": query})

    return "invalid", ""


def _extract_route_info(args_str: str) -> Dict[str, str]:
    """Extract model, skill, instruction from plan_subtask arguments."""
    info = {"model": "", "skill": "direct_answer", "instruction": "", "task_id": "t1"}

    # Try JSON parse
    try:
        obj = json.loads("{" + args_str + "}")
        info.update({k: v for k, v in obj.items() if k in info})
        return info
    except json.JSONDecodeError:
        pass

    # Regex fallback
    for key in ["instruction", "model", "skill", "task_id"]:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', args_str)
        if m:
            info[key] = m.group(1)

    # Router-R1 style: {"model": "X", "query": "Y"}
    try:
        obj = json.loads(args_str)
        if "query" in obj:
            info["instruction"] = obj["query"]
        if "model" in obj:
            info["model"] = obj["model"]
        return info
    except (json.JSONDecodeError, TypeError):
        pass

    return info


class SkillRouterGenerationManager:
    """Multi-turn generation manager for SkillRouter with real API calls.

    Drop-in replacement for Router-R1's LLMGenerationManager.
    """

    def __init__(
        self,
        tokenizer,
        actor_rollout_wg,
        config: GenerationConfig,
        is_validation: bool = False,
    ):
        self.tokenizer = tokenizer
        self.actor_rollout_wg = actor_rollout_wg
        self.config = config
        self.is_validation = is_validation
        self.timing_raw = {}

        self.tensor_fn = TensorHelper(TensorConfig(
            pad_token_id=tokenizer.pad_token_id,
            max_prompt_length=config.max_prompt_length,
            max_obs_length=config.max_obs_length,
            max_start_length=config.max_start_length,
        ))

    def _batch_tokenize(self, texts: List[str]) -> torch.Tensor:
        return self.tokenizer(
            texts, add_special_tokens=False, return_tensors='pt', padding='longest',
        )['input_ids']

    def _postprocess_responses(self, responses: torch.Tensor) -> Tuple[torch.Tensor, List[str]]:
        """Decode and truncate at first action boundary."""
        responses_str = self.tokenizer.batch_decode(responses, skip_special_tokens=True)

        # Truncate at first finish or plan_subtask completion
        truncated = []
        for resp in responses_str:
            # Stop at finish
            if '"name": "finish"' in resp:
                idx = resp.index('"name": "finish"')
                # Find the closing }}
                brace_count = 0
                for j in range(idx, len(resp)):
                    if resp[j] == '{':
                        brace_count += 1
                    elif resp[j] == '}':
                        brace_count -= 1
                    if brace_count < 0 or (brace_count == 0 and resp[j] == '}'):
                        resp = resp[:j+1]
                        break

            # Stop at </answer> or </search>
            for tag in ['</answer>', '</search>']:
                if tag in resp:
                    resp = resp[:resp.index(tag) + len(tag)]
                    break

            truncated.append(resp)

        responses_ids = self._batch_tokenize(truncated)
        return responses_ids, truncated

    def _process_next_obs(self, next_obs: List[str]) -> torch.Tensor:
        ids = self.tokenizer(
            next_obs, padding='longest', return_tensors='pt', add_special_tokens=False,
        )['input_ids']
        if ids.shape[1] > self.config.max_obs_length:
            ids = ids[:, :self.config.max_obs_length]
        return ids

    def _update_rolling_state(self, rollings, cur_responses, next_obs_ids):
        new_input_ids = self.tensor_fn.concatenate_with_padding([
            rollings.batch['input_ids'], cur_responses, next_obs_ids,
        ])
        new_attention_mask = self.tensor_fn.create_attention_mask(new_input_ids)
        new_position_ids = self.tensor_fn.create_position_ids(new_attention_mask)

        effective_len = new_attention_mask.sum(dim=1).max()
        max_len = min(self.config.max_prompt_length, effective_len)

        new_rollings = DataProto.from_dict({
            'input_ids': new_input_ids[:, -max_len:],
            'position_ids': new_position_ids[:, -max_len:],
            'attention_mask': new_attention_mask[:, -max_len:],
        })
        new_rollings.meta_info.update(rollings.meta_info)
        return new_rollings

    def _update_right_side(self, right_side, cur_responses, next_obs_ids=None):
        """Update the right side (response accumulator)."""
        from router_r1.llm_agent.generation import LLMGenerationManager
        # Reuse Router-R1's implementation for tensor concatenation
        pad_id = self.tokenizer.pad_token_id

        if next_obs_ids is not None:
            all_tensors = [right_side['responses'], cur_responses, next_obs_ids]
            all_masked = [right_side['responses_with_info_mask'], cur_responses,
                          torch.full(next_obs_ids.size(), pad_id, dtype=next_obs_ids.dtype)]
        else:
            all_tensors = [right_side['responses'], cur_responses]
            all_masked = [right_side['responses_with_info_mask'], cur_responses]

        concatenated = torch.cat(all_tensors, dim=1)
        concatenated_masked = torch.cat(all_masked, dim=1)

        # Left-pack (move padding to left)
        mask = concatenated != pad_id
        sorted_indices = mask.to(torch.int64).argsort(dim=1, stable=True)
        right_side['responses'] = concatenated.gather(1, sorted_indices)
        right_side['responses_with_info_mask'] = concatenated_masked.gather(1, sorted_indices)
        return right_side

    def _generate_with_gpu_padding(self, rollings_active):
        """Generate with GPU padding for distributed inference."""
        from verl.trainer.ppo.ray_trainer import pad_dataproto_to_divisor, unpad_dataproto
        padded, pad_size = pad_dataproto_to_divisor(rollings_active, self.config.num_gpus)
        gen_output = self.actor_rollout_wg.generate_sequences(padded)
        return unpad_dataproto(gen_output, pad_size=pad_size)

    # ── Core: execute router predictions via real API ────────

    def execute_predictions(
        self, predictions: List[str], pad_token: str, active_mask, do_route: bool = True,
    ) -> Tuple[List[str], List[bool], List[int], List[int], List[float]]:
        """Execute router predictions: call real sub-agent APIs.

        Returns: (next_obs, dones, valid_action, is_route, completion_tokens_cost)
        """
        next_obs = []
        dones = []
        valid_action = []
        is_route = []
        completion_tokens_cost = []  # model_price × output_tokens

        # Collect route requests for batch execution
        route_requests = []  # (index, model, skill, instruction, question)

        for i, (pred, active) in enumerate(zip(predictions, active_mask)):
            if not active:
                next_obs.append('')
                dones.append(True)
                valid_action.append(0)
                is_route.append(0)
                completion_tokens_cost.append(0.0)
                continue

            action_type, content = _parse_action(pred)

            if action_type == "finish":
                next_obs.append('')
                dones.append(True)
                valid_action.append(1)
                is_route.append(0)
                completion_tokens_cost.append(0.0)

            elif action_type == "route" and do_route:
                info = _extract_route_info(content)
                route_requests.append((i, info))
                # Placeholder — will be filled after batch API calls
                next_obs.append(None)
                dones.append(False)
                valid_action.append(1)
                is_route.append(1)
                completion_tokens_cost.append(0.0)

            else:
                # Invalid or route when do_route=False
                next_obs.append('')
                dones.append(False)
                valid_action.append(0)
                is_route.append(0)
                completion_tokens_cost.append(0.0)

        # Batch execute API calls
        if route_requests and do_route:
            def _call_one(args):
                idx, info = args
                model = info.get("model", "gemini-3-flash-preview")
                skill = info.get("skill", "direct_answer")
                instruction = info.get("instruction", "")
                # We don't have the original question in the generation loop,
                # so we pass instruction as both query and question
                response_text, output_tokens = call_sub_agent_api(
                    model, skill, instruction, instruction,
                )
                price = MODEL_COST_PER_M_TOKENS.get(model, 3.0)
                cost = price * max(output_tokens, 1) / 1e6
                return idx, response_text, output_tokens, cost

            with ThreadPool(min(len(route_requests), 32)) as pool:
                results = pool.map(_call_one, route_requests)

            for idx, response_text, output_tokens, cost in results:
                info = route_requests[[r[0] for r in route_requests].index(idx)][1]
                task_id = info.get("task_id", "t1")
                obs = f'\n<obs subtask="{task_id}">{response_text}</obs>\n'
                next_obs[idx] = obs
                completion_tokens_cost[idx] = cost

        # Fill any remaining None obs
        next_obs = [o if o is not None else '' for o in next_obs]

        return next_obs, dones, valid_action, is_route, completion_tokens_cost

    def postprocess_predictions(self, predictions):
        """Parse predictions into (actions, contents) lists."""
        actions = []
        contents = []
        for pred in predictions:
            action_type, content = _parse_action(pred)
            if action_type == "finish":
                actions.append("answer")
            elif action_type == "route":
                actions.append("search")
            else:
                actions.append(None)
            contents.append(content)
        return actions, contents

    def batch_route(self, queries):
        """Not used directly — execute_predictions handles routing."""
        raise NotImplementedError("Use execute_predictions instead")

    # ── Main multi-turn loop ─────────────────────────────────

    def run_llm_loop(self, gen_batch, initial_input_ids: torch.Tensor):
        """Run multi-turn generate → execute → observe loop.

        Same interface as Router-R1's LLMGenerationManager.run_llm_loop().
        """
        original_left_side = {'input_ids': initial_input_ids[:, -self.config.max_start_length:]}
        original_right_side = {
            'responses': initial_input_ids[:, []],
            'responses_with_info_mask': initial_input_ids[:, []],
        }
        batch_completion_tokens = torch.zeros(
            gen_batch.batch['input_ids'].shape[0], dtype=torch.float32,
        )

        active_mask = torch.ones(gen_batch.batch['input_ids'].shape[0], dtype=torch.bool)
        turns_stats = torch.ones(gen_batch.batch['input_ids'].shape[0], dtype=torch.int)
        valid_action_stats = torch.zeros(gen_batch.batch['input_ids'].shape[0], dtype=torch.int)
        valid_route_stats = torch.zeros(gen_batch.batch['input_ids'].shape[0], dtype=torch.int)
        active_num_list = [active_mask.sum().item()]
        rollings = gen_batch

        # Main generation loop
        for step in range(self.config.max_turns):
            if active_mask.sum().item() <= 0:
                break

            rollings.batch = self.tensor_fn.cut_to_effective_len(
                rollings.batch, keys=['input_ids', 'attention_mask', 'position_ids'],
            )

            # Generate from active samples only
            rollings_active = DataProto.from_dict({
                k: v[active_mask] for k, v in rollings.batch.items()
            })
            try:
                gen_output = self._generate_with_gpu_padding(rollings_active)
            except Exception as e:
                print(f"[SkillRouter] Generation error at step {step}: {e}")
                break

            meta_info = gen_output.meta_info
            responses_ids, responses_str = self._postprocess_responses(
                gen_output.batch['responses'],
            )
            responses_ids, responses_str = self.tensor_fn._example_level_pad(
                responses_ids, responses_str, active_mask,
            )

            # Execute predictions (real API calls)
            next_obs, dones, valid_action, is_route_flags, cur_costs = \
                self.execute_predictions(responses_str, self.tokenizer.pad_token, active_mask)

            curr_active_mask = torch.tensor([not d for d in dones], dtype=torch.bool)
            active_mask = active_mask * curr_active_mask
            active_num_list.append(active_mask.sum().item())
            turns_stats[curr_active_mask] += 1
            valid_action_stats += torch.tensor(valid_action, dtype=torch.int)
            valid_route_stats += torch.tensor(is_route_flags, dtype=torch.int)
            batch_completion_tokens += torch.tensor(cur_costs, dtype=torch.float32)

            next_obs_ids = self._process_next_obs(next_obs)

            # Update states
            rollings = self._update_rolling_state(rollings, responses_ids, next_obs_ids)
            original_right_side = self._update_right_side(
                original_right_side, responses_ids, next_obs_ids,
            )

        # Final generation (for samples still active)
        if active_mask.sum().item() > 0:
            rollings.batch = self.tensor_fn.cut_to_effective_len(
                rollings.batch, keys=['input_ids', 'attention_mask', 'position_ids'],
            )
            rollings_active = DataProto.from_dict({
                k: v[active_mask] for k, v in rollings.batch.items()
            })
            gen_output = self._generate_with_gpu_padding(rollings_active)
            responses_ids, responses_str = self._postprocess_responses(
                gen_output.batch['responses'],
            )
            responses_ids, responses_str = self.tensor_fn._example_level_pad(
                responses_ids, responses_str, active_mask,
            )

            _, dones, valid_action, is_route_flags, cur_costs = \
                self.execute_predictions(
                    responses_str, self.tokenizer.pad_token, active_mask, do_route=False,
                )

            curr_active_mask = torch.tensor([not d for d in dones], dtype=torch.bool)
            active_mask = active_mask * curr_active_mask
            active_num_list.append(active_mask.sum().item())
            valid_action_stats += torch.tensor(valid_action, dtype=torch.int)
            valid_route_stats += torch.tensor(is_route_flags, dtype=torch.int)
            batch_completion_tokens += torch.tensor(cur_costs, dtype=torch.float32)

            original_right_side = self._update_right_side(
                original_right_side, responses_ids,
            )

        meta_info['turns_stats'] = turns_stats.tolist()
        meta_info['active_mask'] = active_mask.tolist()
        meta_info['valid_action_stats'] = valid_action_stats.tolist()
        meta_info['valid_route_stats'] = valid_route_stats.tolist()
        meta_info['batch_completion_tokens'] = batch_completion_tokens.tolist()

        print(f"[SkillRouter] ACTIVE_TRAJ_NUM: {active_num_list}")

        return self._compose_final_output(original_left_side, original_right_side, meta_info)

    def _compose_final_output(self, left_side, right_side, meta_info):
        """Compose final output matching verl's expected format."""
        pad_id = self.tokenizer.pad_token_id

        prompts = left_side['input_ids']
        responses = right_side['responses']
        responses_with_info_mask = right_side['responses_with_info_mask']

        # Concatenate prompt + response
        input_ids = self.tensor_fn.concatenate_with_padding([prompts, responses])
        attention_mask = self.tensor_fn.create_attention_mask(input_ids)
        position_ids = self.tensor_fn.create_position_ids(attention_mask)

        # Response mask: only compute loss on response tokens (not info/obs)
        input_ids_with_info_mask = self.tensor_fn.concatenate_with_padding(
            [prompts, responses_with_info_mask],
        )
        response_mask = (input_ids_with_info_mask != pad_id).long()
        # Zero out prompt positions in response_mask
        prompt_len = prompts.shape[1]
        response_mask[:, :prompt_len] = 0

        output = DataProto.from_dict({
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'position_ids': position_ids,
            'responses': responses,
        })
        output.batch['responses_with_info_mask'] = responses_with_info_mask
        output.meta_info = meta_info

        return output
