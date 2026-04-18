from __future__ import annotations
import os
from typing import Any, TypedDict
import yaml

class PoolConfig(TypedDict):
    models: list[str]
    skills: list[str]
    model_skills: dict[str, list[str]]
    cost_per_m: dict[str, float]
    fallbacks: dict[str, str]
    pool_ablations: dict[str, dict[str, Any]]
    policy_models: list[str]
    raw: dict[str, Any]

_POOLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pools.yaml")
_CACHE_BY_PATH: dict[str, PoolConfig] = {}

def _normalize_skill_ids(raw_skills: list[Any], path: str) -> list[str]:
    skill_ids: list[str] = []
    for index, item in enumerate(raw_skills):
        skill_id = item["id"] if isinstance(item, dict) else item
        if not skill_id:
            raise ValueError(f"{path}: skills[{index}] has empty id")
        skill_ids.append(skill_id)
    return skill_ids

def _validate_config(cfg: dict[str, Any], path: str) -> None:
    if not cfg.get("models"):
        raise ValueError(f"{path}: top-level 'models' list is missing or empty")

    skills = _normalize_skill_ids(cfg.get("skills", []), path)
    if not skills:
        raise ValueError(f"{path}: top-level 'skills' list is missing or empty")
    skill_set = set(skills)

    model_ids: set[str] = set()
    for index, model_cfg in enumerate(cfg["models"]):
        model_id = model_cfg.get("id")
        if not model_id:
            raise ValueError(f"{path}: models[{index}] missing required field 'id'")
        if model_id in model_ids:
            raise ValueError(f"{path}: duplicate model id '{model_id}'")
        if "usd_per_1m_output" not in model_cfg:
            raise ValueError(f"{path}: model '{model_id}' missing 'usd_per_1m_output'")
        model_ids.add(model_id)

        for skill in model_cfg.get("allowed_skills", []):
            if skill not in skill_set:
                raise ValueError(f"{path}: model '{model_id}' references unknown skill '{skill}'")

    fallbacks = cfg.get("fallbacks", {})
    if not isinstance(fallbacks, dict):
        raise ValueError(f"{path}: 'fallbacks' must be a mapping")

    for source_id, target_id in fallbacks.items():
        if source_id not in model_ids:
            raise ValueError(f"{path}: fallback source '{source_id}' is not a known model")
        if target_id not in model_ids:
            raise ValueError(f"{path}: fallback target '{target_id}' is not a known model")


def load_pools(path: str | None = None) -> PoolConfig:
    """Load `pools.yaml`, validate it, and return normalized pool metadata."""
    resolved_path = os.path.abspath(path or _POOLS_PATH)
    if resolved_path in _CACHE_BY_PATH:
        return _CACHE_BY_PATH[resolved_path]
    if not os.path.isfile(resolved_path):
        raise FileNotFoundError(f"Pool config not found: {resolved_path}")

    with open(resolved_path) as file:
        cfg = yaml.safe_load(file)
    if not isinstance(cfg, dict):
        raise ValueError(f"{resolved_path}: expected a YAML mapping, got {type(cfg).__name__}")

    _validate_config(cfg, resolved_path)
    skills = _normalize_skill_ids(cfg.get("skills", []), resolved_path)

    result: PoolConfig = {
        "models": [model["id"] for model in cfg["models"]],
        "skills": skills,
        "model_skills": {model["id"]: model.get("allowed_skills", skills) for model in cfg["models"]},
        "cost_per_m": {model["id"]: model["usd_per_1m_output"] for model in cfg["models"]},
        "input_cost_per_m": {model["id"]: model.get("usd_per_1m_input", 0) for model in cfg["models"]},
        "fallbacks": dict(cfg.get("fallbacks", {})),
        "pool_ablations": dict(cfg.get("pool_ablations", {})),
        "policy_models": list(cfg.get("policy_models", [])),
        "raw": cfg,
    }
    _CACHE_BY_PATH[resolved_path] = result
    return result
