"""
Shared configuration — single source of truth for model pool, costs, and eval params.
Matches configs/pools.yaml + agent_system/environments/.../envs.py exactly.
"""

# 9 executor models from pools.yaml
MODEL_POOL = [
    "claude-haiku-4-5-20251001", "gemini-2.5-flash", "kimi-k2.5",
    "claude-sonnet-4-6", "gemini-3.1-pro-preview", "gpt-5.3-codex",
    "qwen3.6-plus", "claude-opus-4-6", "gpt-5.4",
]

# USD per 1M output tokens — from envs.py
COST_PER_M = {
    "claude-haiku-4-5-20251001": 1.25, "gemini-2.5-flash": 1.50,
    "kimi-k2.5": 2.00, "claude-sonnet-4-6": 15.00,
    "gemini-3.1-pro-preview": 10.00, "gpt-5.3-codex": 20.00,
    "qwen3.6-plus": 8.00, "claude-opus-4-6": 75.00, "gpt-5.4": 60.00,
}

TIER = {
    "claude-haiku-4-5-20251001": "nano", "gemini-2.5-flash": "nano",
    "kimi-k2.5": "large", "claude-sonnet-4-6": "mid",
    "gemini-3.1-pro-preview": "large", "gpt-5.3-codex": "code",
    "qwen3.6-plus": "large", "claude-opus-4-6": "large", "gpt-5.4": "large",
}

# 13 skills from pools.yaml
SKILLS = [
    "direct_answer", "reason", "web_search", "database_query",
    "read_document", "read_code", "extract_field", "parse_structured",
    "symbolic_math", "execute_python", "execute_shell", "fact_check", "call_api",
]

# Eval defaults
DEFAULT_API_BASE = "http://localhost:9000/v1"  # External API endpoint
DEFAULT_LOCAL_BASE = "http://localhost:8000/v1"
EVAL_MAX_TOKENS = 4096
SUB_AGENT_TEMP = 0.3


# Fallback for unavailable models (same tier replacement)
MODEL_FALLBACK = {
    "claude-haiku-4-5-20251001": "gemini-2.5-flash",  # nano -> nano
}


def resolve_model(model_id: str) -> str:
    """Return the model ID, falling back if the model is known to be unavailable."""
    return MODEL_FALLBACK.get(model_id, model_id)


def compute_cost(model_id: str, output_tokens: int) -> float:
    """Compute cost using the ORIGINAL model's pricing (not fallback)."""
    return COST_PER_M.get(model_id, 10.0) * max(output_tokens, 1) / 1e6
