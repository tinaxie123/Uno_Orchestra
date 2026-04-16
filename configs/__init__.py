"""Shared config loader — reads configs/pools.yaml as the single source of truth."""

import os
import yaml

_POOLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pools.yaml")
_cache = None


def load_pools(path: str | None = None) -> dict:
    """Load and parse pools.yaml, returning a structured dict.

    Returns:
        {
            "models": ["gemini-2.5-flash", ...],
            "skills": ["direct_answer", ...],
            "model_skills": {"gemini-2.5-flash": [...], ...},
            "cost_per_m": {"gemini-2.5-flash": 0.60, ...},
            "fallbacks": {"claude-haiku-4-5-20251001": "qwen3.6-plus"},
            "raw": <full yaml dict>,
        }
    """
    global _cache
    if _cache is not None and path is None:
        return _cache

    with open(path or _POOLS_PATH) as f:
        cfg = yaml.safe_load(f)

    models = [m["id"] for m in cfg["models"]]
    raw_skills = cfg.get("skills", [])
    skills = [s["id"] if isinstance(s, dict) else s for s in raw_skills]
    model_skills = {m["id"]: m.get("allowed_skills", skills) for m in cfg["models"]}
    cost_per_m = {m["id"]: m["usd_per_1m_output"] for m in cfg["models"]}
    fallbacks = cfg.get("fallbacks", {})

    result = {
        "models": models,
        "skills": skills,
        "model_skills": model_skills,
        "cost_per_m": cost_per_m,
        "fallbacks": fallbacks,
        "raw": cfg,
    }
    if path is None:
        _cache = result
    return result
