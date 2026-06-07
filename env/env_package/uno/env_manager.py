"""
UNO Environment Manager for verl-agent.
Wraps UnoMultiProcessEnv with the verl-agent interface.

Observation rendering matches the Qwen chat-template output the model
saw at SFT time:

    <|im_start|>system\n{schema prompt}<|im_end|>
    <|im_start|>user\nQuestion: {q}<|im_end|>
    <|im_start|>assistant\n<plan>...<route>...</route><|im_end|>
    <|im_start|>tool\n<obs subtask="1">...</obs>\n...<|im_end|>
    <|im_start|>assistant\n

After every env-step we re-render the FULL multi-turn conversation so
the rollout worker's tokenisation matches SFT byte-for-byte; the bit
after the final `<|im_start|>assistant\n` is where the model's next
generation picks up.
"""

import os
from typing import List, Tuple, Dict, Any
import numpy as np
from env.base import EnvironmentManagerBase, to_numpy

# Load system prompt once
_SYSTEM_PROMPT_PATH = os.environ.get(
    "UNO_SYSTEM_PROMPT",
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../configs/uno/system_prompt.txt")
    ),
)
try:
    with open(_SYSTEM_PROMPT_PATH) as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    SYSTEM_PROMPT = ""
    print(f"WARNING: System prompt not found at {_SYSTEM_PROMPT_PATH}")


def _chat_turn(role: str, content: str) -> str:
    """Render one Qwen chat-template turn."""
    return f"<|im_start|>{role}\n{content}<|im_end|>\n"


def _render_conversation(
    question: str,
    assistant_turns: List[str],
    tool_turns: List[str],
    open_assistant: bool = True,
) -> str:
    """Render system/user/(assistant/tool)*... with trailing assistant open.

    - assistant_turns[i]: what the model emitted in the i-th assistant turn
    - tool_turns[i]:      what the env injected in the i-th tool turn
    - tool_turns is always one shorter or equal length to assistant_turns:
        A0 T0 A1 T1 ... A_{n-1} T_{n-1}     (n pairs, env just injected T)
        A0 T0 A1 T1 ... A_{n-1}             (n assistants, n-1 tools)
    When open_assistant is True, finish with `<|im_start|>assistant\\n`
    to cue the model to continue.
    """
    parts = [
        _chat_turn("system", SYSTEM_PROMPT),
        _chat_turn("user", f"Question: {question}"),
    ]
    for i, a in enumerate(assistant_turns):
        parts.append(_chat_turn("assistant", a))
        if i < len(tool_turns):
            parts.append(_chat_turn("tool", tool_turns[i]))
    if open_assistant:
        parts.append("<|im_start|>assistant\n")
    return "".join(parts)


class UnoEnvironmentManager(EnvironmentManagerBase):
    """
    EnvironmentManager for UNO.

    Each env maintains a pair of parallel lists per sample:
      - assistant_turns[i]: list of prior model outputs (plan+route blocks)
      - tool_turns[i]:      list of prior env-injected obs blocks
    After every env-step we emit the next observation as the full
    rendered Qwen chat template up to the next `<|im_start|>assistant\\n`,
    so the rollout worker's tokenisation is identical to what SFT saw.
    """

    def __init__(self, envs, projection_f, config):
        super().__init__(envs, projection_f, config)
        self.questions: List[str] = []
        self.assistant_turns: List[List[str]] = []
        self.tool_turns: List[List[str]] = []

    def reset(self, kwargs) -> Tuple[Dict[str, Any], List[Dict]]:
        obs, infos = self.envs.reset(kwargs=kwargs)
        self.questions = list(obs)
        n = len(obs)
        self.assistant_turns = [[] for _ in range(n)]
        self.tool_turns = [[] for _ in range(n)]

        observations = {
            "text": [_render_conversation(q, [], [], open_assistant=True)
                     for q in self.questions],
            "image": None,
            "anchor": list(obs),
        }
        return observations, infos

    def step(self, text_actions: List[str]):
        actions, valids = self.projection_f(text_actions)
        next_obs, rewards, dones, infos = self.envs.step(actions)

        # 1. record the assistant turn the model just produced (the full
        #    plan+route block) — SFT put this in assistant role, so do we.
        # 2. record the tool turn the env just built (one or more <obs>).
        for i in range(len(next_obs)):
            if i < len(self.assistant_turns):
                # text_actions[i] is the raw model output; keep it
                # verbatim so the rendered turn matches what Qwen chat
                # template would produce.
                self.assistant_turns[i].append(text_actions[i])
                if next_obs[i]:
                    self.tool_turns[i].append(next_obs[i].strip())

        anchor = [obs if obs else "" for obs in next_obs]

        # Render full conversation up to the next open assistant cue.
        next_text = [
            _render_conversation(
                self.questions[i],
                self.assistant_turns[i],
                self.tool_turns[i],
                open_assistant=True,
            )
            for i in range(len(next_obs))
        ]

        next_observations = {
            "text": next_text,
            "image": None,
            "anchor": anchor,
        }

        for i, info in enumerate(infos):
            info["is_action_valid"] = to_numpy(valids[i])

        rewards = to_numpy(rewards)
        dones = to_numpy(dones)
        return next_observations, rewards, dones, infos

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
