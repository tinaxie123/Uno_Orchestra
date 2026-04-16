"""Shared config loader — reads configs/pools.yaml as the single source of truth.

Single entry point for all pool configuration.  Every module that needs
model/skill information should ``from configs import load_pools`` and use
the returned ``PoolConfig`` TypedDict rather than parsing YAML directly.

Return structure
----------------
models          : list[str]         — ordered model IDs
skills          : list[str]         — ordered skill IDs
model_skills    : dict[str, list]   — model → allowed skills
cost_per_m      : dict[str, float]  — model → USD per 1M output tokens
fallbacks       : dict[str, str]    — model → fallback model (may be empty)
pool_ablations  : dict[str, dict]   — ablation name → {include/exclude}
policy_models   : list[str]         — models to be trained as routers
raw             : dict              — full parsed YAML (escape hatch)
"""

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
_cache: dict[str, PoolConfig] = {}


def _validate(cfg: dict[str, Any], path: str) -> None:
    """Fail fast on missing/inconsistent fields — errors surface here, not downstream."""
    if "models" not in cfg or not cfg["models"]:
        raise ValueError(f"{path}: top-level 'models' list is missing or empty")

    skill_ids: set[str] = set()
    for i, s in enumerate(cfg.get("skills", [])):
        sid = s["id"] if isinstance(s, dict) else s
        if not sid:
            raise ValueError(f"{path}: skills[{i}] has no id")
        skill_ids.add(sid)

    if not skill_ids:
        raise ValueError(f"{path}: 'skills' list is missing or empty")

    model_ids: set[str] = set()
    for i, m in enumerate(cfg["models"]):
        if "id" not in m:
            raise ValueError(f"{path}: models[{i}] missing required field 'id'")
        if "usd_per_1m_output" not in m:
            raise ValueError(f"{path}: models[{i}] ('{m['id']}') missing 'usd_per_1m_output'")
        mid = m["id"]
        if mid in model_ids:
            raise ValueError(f"{path}: duplicate model id '{mid}'")
        model_ids.add(mid)

        for skill in m.get("allowed_skills", []):
            if skill not in skill_ids:
                raise ValueError(
                    f"{path}: model '{mid}' references unknown skill '{skill}'. "
                    f"Valid skills: {sorted(skill_ids)}"
                )

    for src, tgt in cfg.get("fallbacks", {}).items():
        if tgt not in model_ids:
            raise ValueError(
                f"{path}: fallback '{src}' → '{tgt}' references unknown target model. "
                f"Valid models: {sorted(model_ids)}"
            )


def load_pools(path: str | None = None) -> PoolConfig:
    """Load and validate pools.yaml, returning a structured dict.

    Results are cached by resolved file path — repeated calls with the same
    path hit memory, not disk.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError``
    on any schema violation (missing fields, unknown references, etc.).
    """
    resolved = os.path.abspath(path or _POOLS_PATH)

    if resolved in _cache:
        return _cache[resolved]

    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Pool config not found: {resolved}")

    with open(resolved) as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError(f"{resolved}: expected a YAML mapping, got {type(cfg).__name__}")

    _validate(cfg, resolved)

    # --- Build normalised structure ---
    models = [m["id"] for m in cfg["models"]]
    skills = [s["id"] if isinstance(s, dict) else s for s in cfg.get("skills", [])]
    skill_set = set(skills)
    model_skills = {
        m["id"]: m.get("allowed_skills", list(skill_set))
        for m in cfg["models"]
    }
    cost_per_m = {m["id"]: m["usd_per_1m_output"] for m in cfg["models"]}
    fallbacks: dict[str, str] = cfg.get("fallbacks", {})
    pool_ablations: dict[str, dict] = cfg.get("pool_ablations", {})
    policy_models: list[str] = cfg.get("policy_models", [])

    result = PoolConfig(
        models=models,
        skills=skills,
        model_skills=model_skills,
        cost_per_m=cost_per_m,
        fallbacks=fallbacks,
        pool_ablations=pool_ablations,
        policy_models=policy_models,
        raw=cfg,
    )
    _cache[resolved] = result
    return result
