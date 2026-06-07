"""Default composed implementations for public UNO skill names.

The public route schema remains unchanged: the model still emits
`skill="<primitive-name>"`.  These classes let the harness implement selected
public skill names as recipes over lower-level primitive calls.
"""

from __future__ import annotations

from uno_orchestor.routing.uno.primitives import PrimitiveResult, Route
from uno_orchestor.routing.uno.skills.base import SkillContext, SkillImplementation


class SymbolicMathSkill:
    id = "symbolic_math"

    def run(self, route: Route, ctx: SkillContext) -> PrimitiveResult:
        exact = ctx.run_primitive("symbolic_math", route.query)
        if _is_error(exact):
            return exact

        check = ctx.run_primitive(
            "execute_python",
            "print('symbolic_result:', repr(%r))" % exact.text,
        )
        if _is_error(check):
            return _with_backend(exact, self.id)

        return _combine(
            self.id,
            [
                ("exact", exact),
                ("check", check),
            ],
        )


class ExtractFieldSkill:
    id = "extract_field"

    def run(self, route: Route, ctx: SkillContext) -> PrimitiveResult:
        parsed = ctx.run_primitive("parse_structured", route.query)
        if not _is_error(parsed):
            extracted = ctx.run_primitive(
                "extract_field",
                f"{route.query}\n\nParsed payload:\n{parsed.text}",
            )
            if not _is_error(extracted):
                return _combine(
                    self.id,
                    [
                        ("parsed", parsed),
                        ("extracted", extracted),
                    ],
                )

        direct = ctx.run_primitive("extract_field", route.query)
        return _with_backend(direct, self.id) if not _is_error(direct) else direct


class FactCheckSkill:
    id = "fact_check"

    def run(self, route: Route, ctx: SkillContext) -> PrimitiveResult:
        evidence = ctx.run_primitive("web_search", route.query)
        if _is_error(evidence):
            verdict = ctx.run_primitive("fact_check", route.query)
            return _with_backend(verdict, self.id) if not _is_error(verdict) else verdict

        verdict = ctx.run_primitive(
            "fact_check",
            f"Claim:\n{route.query}\n\nEvidence snippets:\n{evidence.text}",
        )
        if _is_error(verdict):
            return _with_backend(evidence, self.id)

        return _combine(
            self.id,
            [
                ("evidence", evidence),
                ("verdict", verdict),
            ],
        )


def build_default_skills() -> dict[str, SkillImplementation]:
    skills: list[SkillImplementation] = [
        SymbolicMathSkill(),
        ExtractFieldSkill(),
        FactCheckSkill(),
    ]
    return {skill.id: skill for skill in skills}


def _combine(skill_id: str, parts: list[tuple[str, PrimitiveResult]]) -> PrimitiveResult:
    text = "\n".join(
        f"<{name} backend=\"{result.backend}\">{result.text}</{name}>"
        for name, result in parts
    )
    return PrimitiveResult(
        text=text,
        output_tokens=sum(part.output_tokens for _, part in parts),
        billable=any(part.billable for _, part in parts),
        backend=f"skill:{skill_id}",
        timed_out=any(part.timed_out for _, part in parts),
    )


def _with_backend(result: PrimitiveResult, skill_id: str) -> PrimitiveResult:
    return PrimitiveResult(
        text=result.text,
        output_tokens=result.output_tokens,
        billable=result.billable,
        backend=f"skill:{skill_id}/{result.backend}",
        timed_out=result.timed_out,
    )


def _is_error(result: PrimitiveResult) -> bool:
    return result.text.lstrip().startswith("<error ")
