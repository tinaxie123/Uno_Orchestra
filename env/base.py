"""Minimal environment-manager base utilities used by Uno rollouts."""

from __future__ import annotations

from typing import Any

import numpy as np


def to_numpy(value: Any):
    if isinstance(value, np.ndarray):
        return value
    try:
        import torch

        if isinstance(value, torch.Tensor):
            return value.detach().cpu().numpy()
    except Exception:
        pass
    return np.asarray(value)


class EnvironmentManagerBase:
    def __init__(self, envs, projection_f, config):
        self.envs = envs
        self.projection_f = projection_f
        self.config = config
