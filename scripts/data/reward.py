from __future__ import annotations
import math
from collections import deque


class CostNormalizer:
   
    def __init__(self, window: int = 1000, lo_pct: float = 5, hi_pct: float = 95):
        self.buffer: deque[float] = deque(maxlen=window)
        self.lo_pct = lo_pct
        self.hi_pct = hi_pct

    def normalize(self, raw_cost: float) -> float:
        """Return normalized cost in [0, 1] where 1 = cheapest."""
        self.buffer.append(raw_cost)

        if len(self.buffer) < 2:
            return 0.5

        sorted_buf = sorted(self.buffer)
        n = len(sorted_buf)
        lo = sorted_buf[int(n * self.lo_pct / 100)]
        hi = sorted_buf[int(n * self.hi_pct / 100)]
        if hi <= lo:
            return 0.5
        val = math.sqrt(raw_cost)
        lo_s = math.sqrt(lo)
        hi_s = math.sqrt(hi)

        if hi_s <= lo_s:
            return 0.5

        clipped = max(lo_s, min(val, hi_s))
        scaled = (clipped - lo_s) / (hi_s - lo_s)

        return 1.0 - scaled

_cost_normalizer = CostNormalizer()


def compute_step_reward(
    result_useful: bool,
    model_cost_per_m: float,
    output_tokens: int = 256,
    cost_coe: float = 0.1,
) -> float:
   
    raw_cost = model_cost_per_m * max(output_tokens, 1) / 1e6
    api_cost = _cost_normalizer.normalize(raw_cost)

    if result_useful:
        return 1.0 * (1.0 - cost_coe) + api_cost * cost_coe
    else:
        return 0.0


def compute_episode_reward(
    answer_correct: bool,
    total_cost: float,
    format_valid: bool = True,
    cost_coe: float = 0.1,
) -> float:
   
    if not format_valid:
        return -1.0

    api_cost = _cost_normalizer.normalize(total_cost)

    if answer_correct:
        return 1.0 * (1.0 - cost_coe) + api_cost * cost_coe
    else:
        return 0.0


def score_trajectory(
    correct: bool,
    routing_decisions: list[dict],
    cost_per_m: dict[str, float],
    cost_coe: float = 0.1,
) -> dict:
    step_rewards = []
    total_cost = 0.0

    for rd in routing_decisions:
        model = rd.get("routed_model", "")
        useful = rd.get("useful", False)
        model_price = cost_per_m.get(model, 10.0)
        total_cost += model_price * 256 / 1e6  

        sr = compute_step_reward(
            result_useful=useful,
            model_cost_per_m=model_price,
            cost_coe=cost_coe,
        )
        step_rewards.append(sr)

    episode_reward = compute_episode_reward(
        answer_correct=correct,
        total_cost=total_cost,
        cost_coe=cost_coe,
    )

    return {
        "episode_reward": episode_reward,
        "step_rewards": step_rewards,
        "total_cost_usd": total_cost,
        "avg_step_reward": sum(step_rewards) / max(len(step_rewards), 1),
        "n_steps": len(step_rewards),
    }
