"""UNO route harness.

This module is the boundary between the RL/eval environment and primitive
implementations.  Environments hand it parsed routes; it validates the route,
chooses a backend, enforces a hard timeout, and returns a normalized
PrimitiveResult.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from configs import load_pools
from uno_orchestor.routing.uno.backends import (
    LangChainSubAgentBackend,
    LocalPrimitiveBackend,
    PrimitiveBackend,
)
from uno_orchestor.routing.uno.primitives import (
    PRIMITIVES,
    VALID_PRIMITIVES,
    PrimitiveResult,
    Route,
)
from uno_orchestor.routing.uno.skills import SkillContext, SkillImplementation, build_default_skills

logger = logging.getLogger(__name__)


class RouteValidationError(ValueError):
    pass


@dataclass(frozen=True)
class HarnessConfig:
    valid_models: frozenset[str]
    valid_skills: frozenset[str]
    model_skills: Mapping[str, frozenset[str]]
    cost_per_m_tokens: Mapping[str, float]


class RouteHarness:
    def __init__(
        self,
        config: HarnessConfig,
        backends: Sequence[PrimitiveBackend],
        skills: Mapping[str, SkillImplementation] | None = None,
        hard_timeout_sec: float | None = None,
        timeout_pool_size: int | None = None,
    ):
        self.config = config
        self.backends = list(backends)
        self.skills = dict(skills or {})
        self.hard_timeout_sec = float(hard_timeout_sec or os.environ.get("UNO_SUBAGENT_HARD_TIMEOUT_SEC", "150"))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(timeout_pool_size or os.environ.get("UNO_SUBAGENT_TIMEOUT_POOL_SIZE", "256")),
            thread_name_prefix="uno-route-harness",
        )

    def validate(self, route: Route) -> None:
        error = validate_route(route.model, route.skill, self.config.valid_models, self.config.model_skills)
        if error:
            raise RouteValidationError(error)

    def run_route(self, route: Route, question: str) -> PrimitiveResult:
        self.validate(route)
        fut = self._executor.submit(self._run_route_without_timeout, route, question)
        try:
            return fut.result(timeout=self.hard_timeout_sec)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "uno route hard timeout after %.1fs (model=%s skill=%s query=%r)",
                self.hard_timeout_sec,
                route.model,
                route.skill,
                route.query[:80],
            )
            return PrimitiveResult(
                f"<error reason=\"primitive_hard_timeout\" after_s=\"{self.hard_timeout_sec}\"/>",
                backend="timeout",
                timed_out=True,
            )

    def _run_route_without_timeout(self, route: Route, question: str) -> PrimitiveResult:
        skill = self.skills.get(route.skill)
        if skill is not None:
            ctx = SkillContext(
                question=question,
                run_primitive=lambda primitive, query: self.run_primitive(
                    Route(
                        round=route.round,
                        subtask=route.subtask,
                        model=route.model,
                        skill=primitive,
                        query=query,
                    ),
                    question,
                ),
            )
            return skill.run(route, ctx)

        return self.run_primitive(route, question)

    def run_primitive(self, route: Route, question: str) -> PrimitiveResult:
        """Run one primitive backend directly, bypassing composed skills.

        This is the internal entrypoint used by skill recipes.  Public model
        and model-skill validation happens in run_route(); internal recipe
        calls only require that the primitive exists in the closed vocabulary.
        """
        if route.skill not in PRIMITIVES:
            return PrimitiveResult(
                f"<error reason=\"unknown_primitive\" primitive=\"{_xml_attr(route.skill)}\"/>",
                backend="harness",
            )

        errors = []
        for backend in self.backends:
            try:
                result = backend.run(route, question)
            except Exception as exc:
                errors.append(f"{backend.name}: {type(exc).__name__}: {str(exc)[:200]}")
                continue
            if result is not None:
                return result

        if errors:
            detail = " | ".join(errors)
            return PrimitiveResult(
                f"<error reason=\"primitive_backend_failed\" primitive=\"{_xml_attr(route.skill)}\">"
                f"{_xml_text(detail)}</error>",
                backend="harness",
            )
        return PrimitiveResult(
            f"<error reason=\"primitive_backend_not_configured\" primitive=\"{_xml_attr(route.skill)}\"/>",
            backend="harness",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False)


def load_harness_config() -> HarnessConfig:
    pool = load_pools()
    return HarnessConfig(
        valid_models=frozenset(pool["models"]),
        valid_skills=frozenset(pool["skills"]),
        model_skills={
            model: frozenset(skills) for model, skills in pool["model_skills"].items()
        },
        cost_per_m_tokens=dict(pool["cost_per_m"]),
    )


def build_default_harness(
    api_key: str | None = None,
    base_url: str | None = None,
    model_resolver: Callable[[str], str] | None = None,
) -> RouteHarness:
    config = load_harness_config()
    model_max_tokens = {
        "gemini-2.5-flash-lite": 256,
        "gemini-2.5-flash": 256,
        "kimi-k2.5": 512,
        "gemini-3-flash-preview": 512,
        "claude-sonnet-4-6": 512,
        "claude-opus-4-6": 768,
        "gpt-5.4": 768,
        "gpt-5.3-codex": 1024,
    }
    return RouteHarness(
        config=config,
        backends=[
            LocalPrimitiveBackend(),
            LangChainSubAgentBackend(
                model_max_tokens=model_max_tokens,
                api_key=api_key,
                base_url=base_url,
                model_resolver=model_resolver,
            ),
        ],
        skills=build_default_skills(),
    )


def validate_route(
    model: str,
    skill: str,
    valid_models: Iterable[str],
    model_skills: Mapping[str, Iterable[str]] | None = None,
) -> str | None:
    if model not in set(valid_models):
        return f"unknown model '{model}'"
    if skill not in VALID_PRIMITIVES:
        return f"unknown primitive '{skill}'"
    if skill not in PRIMITIVES:
        return f"primitive '{skill}' is not implemented"
    if model_skills is not None:
        allowed = set(model_skills.get(model, ()))
        if allowed and skill not in allowed:
            return f"primitive '{skill}' is not allowed for model '{model}'"
    return None


def _xml_attr(text: str) -> str:
    return _xml_text(text).replace('"', "&quot;")


def _xml_text(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
