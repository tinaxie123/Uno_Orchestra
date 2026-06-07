import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from configs import load_pools
_pools = load_pools()
MODEL_POOL = _pools["models"]
COST_PER_M = _pools["cost_per_m"]
INPUT_COST_PER_M = _pools.get("input_cost_per_m", {})
SKILLS = _pools["skills"]
MODEL_FALLBACK = _pools["fallbacks"]
DEFAULT_API_BASE = "http://localhost:9000/v1"
DEFAULT_LOCAL_BASE = "http://localhost:8000/v1"
EVAL_MAX_TOKENS = 4096
SUB_AGENT_TEMP = 0.3


def resolve_model(model_id: str) -> str:
    """Return the model ID, falling back if the model is known to be unavailable."""
    return MODEL_FALLBACK.get(model_id, model_id)


def compute_cost(model_id: str, output_tokens: int, input_tokens: int = 0) -> float:
    """Compute cost using the ORIGINAL model's pricing (not fallback)."""
    out_cost = COST_PER_M.get(model_id, 10.0) * max(output_tokens, 0) / 1e6
    in_cost = INPUT_COST_PER_M.get(model_id, 0.0) * max(input_tokens, 0) / 1e6
    if output_tokens <= 0 and input_tokens <= 0:
        out_cost = COST_PER_M.get(model_id, 10.0) / 1e6
    return out_cost + in_cost
