"""Small batched memory helpers for legacy environment managers."""

from __future__ import annotations


class SimpleMemory:
    def __init__(self):
        self._rows: list[list[dict]] = []

    def reset(self, batch_size: int):
        self._rows = [[] for _ in range(batch_size)]

    def store(self, payload: dict):
        if not self._rows:
            return
        for i in range(len(self._rows)):
            item = {}
            for key, value in payload.items():
                if isinstance(value, list) and len(value) == len(self._rows):
                    item[key] = value[i]
                else:
                    item[key] = value
            self._rows[i].append(item)

    def fetch(self, history_length: int, obs_key: str = "text_obs", action_key: str = "action"):
        contexts = []
        valid_lens = []
        for row in self._rows:
            recent = row[-history_length:] if history_length > 0 else []
            valid_lens.append(len(recent))
            parts = []
            for item in recent:
                if action_key in item:
                    parts.append(f"Action: {item[action_key]}")
                if obs_key in item:
                    parts.append(f"Observation: {item[obs_key]}")
            contexts.append("\n".join(parts))
        return contexts, valid_lens

    def __getitem__(self, index: int):
        return self._rows[index]


class SearchMemory(SimpleMemory):
    pass
