"""
Trajectory schema validator for schema.

Single source of truth: data/trajectory_schema.md

Validates a ChatML messages list against all 16 hard rules in schema §3,
plus closed-vocabulary checks for `model` and `skill` (when a pools dict is
provided). Returns a structured ValidationResult with errors, warnings, and
trajectory statistics.

Usage
-----
    from scripts.validate_schema import validate_messages, load_pools

    pools = load_pools("config/pools.yaml")  # optional
    result = validate_messages(messages, pools=pools)
    if not result.valid:
        for err in result.errors:
            print(err.code, err.message)

Design notes
------------
- `pools` is optional. If None, closed-vocabulary checks are emitted as
  warnings (code starts with "WARN_") instead of errors. This lets the
  validator run before config/pools.yaml exists.
- Error codes are stable strings, useful for filtering / aggregating dry-run
  results.
- All 16 rules from trajectory_schema.md §3 are implemented as named methods on
  the `_Validator` class for easy reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Regex (verbatim from trajectory_schema.md §8)
# ---------------------------------------------------------------------------

PLAN_RE = re.compile(r'<plan round="(\d+)">(.*?)</plan>', re.DOTALL)
SUBTASK_RE = re.compile(
    r'<subtask id="(\d+)" depends_on="([^"]*)">(.*?)</subtask>', re.DOTALL
)
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
OBS_RE = re.compile(r'<obs subtask="(\d+)">(.*?)</obs>', re.DOTALL)
VERIFY_RE = re.compile(
    r'<verify round="(\d+)" status="(pass|repair_needed)"(?: target="([^"]*)")?>(.*?)</verify>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

# Tags that must NEVER appear inside tool turns (schema §3, rule 16).
ASSISTANT_ONLY_TAGS = (
    "<plan ",
    "<subtask ",
    "<route ",
    "<verify ",
    "<final_answer>",
)
# Tag that must NEVER appear outside tool turns (rule 15).
TOOL_ONLY_TAG = "<obs "

# Hard caps (rules 13, 14).
MAX_ROUTES = 8
MAX_PLAN_ROUNDS = 3

# Allowed verify statuses.
VERIFY_STATUSES = {"pass", "repair_needed"}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    code: str
    message: str
    turn_index: int | None = None  # which message turn the error occurred on


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add_error(self, code: str, message: str, turn_index: int | None = None) -> None:
        self.errors.append(ValidationError(code, message, turn_index))
        self.valid = False

    def add_warning(self, code: str, message: str, turn_index: int | None = None) -> None:
        self.warnings.append(ValidationError(code, message, turn_index))


# ---------------------------------------------------------------------------
# Internal parsed structures
# ---------------------------------------------------------------------------


@dataclass
class ParsedSubtask:
    id: int
    depends_on: list[int]
    text: str
    declared_in_round: int


@dataclass
class ParsedRoute:
    round: int
    subtask_id: int
    model: str
    skill: str
    text: str
    turn_index: int


@dataclass
class ParsedObs:
    subtask_id: int
    text: str
    turn_index: int


@dataclass
class ParsedVerify:
    round: int
    status: str
    target: list[int]  # parsed ascending; empty if status=pass
    text: str
    turn_index: int


@dataclass
class ParsedTrajectory:
    plan_rounds: list[int] = field(default_factory=list)  # rounds in declaration order
    subtasks: dict[int, ParsedSubtask] = field(default_factory=dict)
    routes: list[ParsedRoute] = field(default_factory=list)
    obs_list: list[ParsedObs] = field(default_factory=list)
    verifies: list[ParsedVerify] = field(default_factory=list)
    finals: list[tuple[int, str]] = field(default_factory=list)  # (turn_index, text)
    is_lazy: bool = False  # 0 plans, 0 routes, just final answer


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_messages(
    messages: list[dict],
    pools: dict | None = None,
) -> ValidationResult:
    """
    Validate a ChatML trajectory against schema.

    Parameters
    ----------
    messages : list of {"role": str, "content": str}
        ChatML conversation. Roles must be "system", "user", "assistant", or
        "tool". The system/user turns are not validated structurally beyond
        ensuring tags don't leak into them.
    pools : dict, optional
        Should contain "available_models" and "available_skills" keys, each a
        list of allowed strings. If None, closed-vocabulary checks degrade to
        warnings.

    Returns
    -------
    ValidationResult
    """
    return _Validator(messages, pools).run()


def load_pools(path: str) -> dict:
    """
    Load a config/pools.yaml file (v1 format).

    Returns a dict with:
        available_models     : list[str] of model ids
        available_skills     : list[str] of skill ids
        model_allowed_skills : dict[model_id -> list[skill_id]]
        deprecated_models    : set[model_id] flagged as deprecated
        policy_model         : str or None (forbidden as a route target)
        enforcement          : "strict" | "free"  (controls allowed_skills check)
    """
    import yaml  # noqa: WPS433

    with open(path) as f:
        data = yaml.safe_load(f)

    models_raw = data.get("models", data.get("available_models", []))
    skills_raw = data.get("skills", data.get("available_skills", []))

    available_models: list[str] = []
    model_allowed_skills: dict[str, list[str]] = {}
    deprecated_models: set[str] = set()
    for m in models_raw:
        if isinstance(m, str):
            available_models.append(m)
            continue
        mid = m["id"]
        available_models.append(mid)
        model_allowed_skills[mid] = list(m.get("allowed_skills", []))
        if m.get("deprecated", False):
            deprecated_models.add(mid)

    available_skills: list[str] = []
    for s in skills_raw:
        if isinstance(s, str):
            available_skills.append(s)
        else:
            available_skills.append(s["id"])

    return {
        "available_models": available_models,
        "available_skills": available_skills,
        "model_allowed_skills": model_allowed_skills,
        "deprecated_models": deprecated_models,
        "policy_model": data.get("policy_model"),
        "enforcement": data.get("enforcement", "strict"),
    }


# ---------------------------------------------------------------------------
# Validator implementation
# ---------------------------------------------------------------------------


class _Validator:
    def __init__(self, messages: list[dict], pools: dict | None) -> None:
        self.messages = messages
        self.pools = pools
        self.result = ValidationResult(valid=True)
        self.parsed = ParsedTrajectory()

    def run(self) -> ValidationResult:
        # Phase 1: structural pre-checks (roles, tag-in-wrong-role, etc.)
        if not self._check_role_structure():
            # Even if role checks fail, keep going to surface more errors.
            pass

        # Phase 2: parse all tags into the ParsedTrajectory.
        self._parse_messages()

        # Phase 3: run all 16 rules. Order matches schema §3.
        self._rule_01_exactly_one_final()
        self._rule_02_first_assistant_turn()
        self._rule_03_plan_round_monotonic()
        self._rule_04_subtask_id_monotonic()
        self._rule_05_depends_on_validity()
        self._rule_06_route_obs_pairing()
        self._rule_07_route_round_matches_plan()
        self._rule_08_verify_position()
        self._rule_09_verify_pass_followed_by_final()
        self._rule_10_verify_repair_followed_by_plan()
        self._rule_11_verify_target_ascending_declared()
        self._rule_12_closed_vocab_model_skill()
        self._rule_13_route_count_cap()
        self._rule_14_plan_round_count_cap()
        # Rules 15 and 16 are enforced in _check_role_structure / _parse_messages.

        # Phase 4: compute stats for downstream analysis.
        self._compute_stats()

        return self.result

    # ------------------------------------------------------------------
    # Phase 1: role structure
    # ------------------------------------------------------------------

    def _check_role_structure(self) -> bool:
        ok = True
        for i, msg in enumerate(self.messages):
            role = msg.get("role")
            content = msg.get("content", "")
            if not isinstance(content, str):
                self.result.add_error(
                    "INVALID_CONTENT_TYPE",
                    f"Turn {i}: content must be str, got {type(content).__name__}",
                    turn_index=i,
                )
                ok = False
                continue

            if role == "tool":
                # Rule 16: assistant-only tags must not appear in tool turns.
                for tag in ASSISTANT_ONLY_TAGS:
                    if tag in content:
                        self.result.add_error(
                            "ASSISTANT_TAG_IN_TOOL_TURN",
                            f"Turn {i}: tag '{tag.strip()}' appeared in a tool turn",
                            turn_index=i,
                        )
                        ok = False
            elif role == "assistant":
                # Rule 15: <obs ...> must not appear in assistant turns.
                if TOOL_ONLY_TAG in content:
                    self.result.add_error(
                        "OBS_TAG_IN_ASSISTANT_TURN",
                        f"Turn {i}: '<obs ...>' appeared in an assistant turn",
                        turn_index=i,
                    )
                    ok = False
            elif role in ("system", "user"):
                # Schema tags should not leak into system/user content.
                for tag in ASSISTANT_ONLY_TAGS + (TOOL_ONLY_TAG,):
                    if tag in content:
                        self.result.add_warning(
                            "WARN_SCHEMA_TAG_IN_SYSTEM_OR_USER",
                            f"Turn {i} (role={role}): tag '{tag.strip()}' leaked into {role} content",
                            turn_index=i,
                        )
            else:
                self.result.add_error(
                    "UNKNOWN_ROLE",
                    f"Turn {i}: unknown role '{role}' (expected system/user/assistant/tool)",
                    turn_index=i,
                )
                ok = False
        return ok

    # ------------------------------------------------------------------
    # Phase 2: parse
    # ------------------------------------------------------------------

    def _parse_messages(self) -> None:
        for i, msg in enumerate(self.messages):
            role = msg.get("role")
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue

            if role == "assistant":
                self._parse_assistant_turn(content, turn_index=i)
            elif role == "tool":
                self._parse_tool_turn(content, turn_index=i)

    def _parse_assistant_turn(self, content: str, turn_index: int) -> None:
        # Parse plans (with their nested subtasks).
        for m in PLAN_RE.finditer(content):
            round_num = int(m.group(1))
            inner = m.group(2)
            self.parsed.plan_rounds.append(round_num)
            for sm in SUBTASK_RE.finditer(inner):
                sid = int(sm.group(1))
                deps_str = sm.group(2)
                if deps_str.strip() == "":
                    deps: list[int] = []
                else:
                    try:
                        deps = [int(x) for x in deps_str.split(",")]
                    except ValueError:
                        self.result.add_error(
                            "INVALID_DEPENDS_ON_FORMAT",
                            f"Turn {turn_index}: subtask {sid} has malformed depends_on='{deps_str}'",
                            turn_index=turn_index,
                        )
                        deps = []
                if sid in self.parsed.subtasks:
                    self.result.add_error(
                        "SUBTASK_ID_REUSED",
                        f"Turn {turn_index}: subtask id={sid} reused (rule 4)",
                        turn_index=turn_index,
                    )
                else:
                    self.parsed.subtasks[sid] = ParsedSubtask(
                        id=sid,
                        depends_on=deps,
                        text=sm.group(3).strip(),
                        declared_in_round=round_num,
                    )

        # Parse routes.
        for m in ROUTE_RE.finditer(content):
            self.parsed.routes.append(
                ParsedRoute(
                    round=int(m.group(1)),
                    subtask_id=int(m.group(2)),
                    model=m.group(3),
                    skill=m.group(4),
                    text=m.group(5).strip(),
                    turn_index=turn_index,
                )
            )

        # Parse verifies.
        for m in VERIFY_RE.finditer(content):
            round_num = int(m.group(1))
            status = m.group(2)
            target_str = m.group(3) or ""
            if target_str.strip() == "":
                target: list[int] = []
            else:
                try:
                    target = [int(x) for x in target_str.split(",")]
                except ValueError:
                    self.result.add_error(
                        "INVALID_VERIFY_TARGET_FORMAT",
                        f"Turn {turn_index}: verify round={round_num} has malformed target='{target_str}'",
                        turn_index=turn_index,
                    )
                    target = []
            self.parsed.verifies.append(
                ParsedVerify(
                    round=round_num,
                    status=status,
                    target=target,
                    text=m.group(4).strip(),
                    turn_index=turn_index,
                )
            )

        # Parse final answers.
        for m in FINAL_RE.finditer(content):
            self.parsed.finals.append((turn_index, m.group(1).strip()))

    def _parse_tool_turn(self, content: str, turn_index: int) -> None:
        for m in OBS_RE.finditer(content):
            self.parsed.obs_list.append(
                ParsedObs(
                    subtask_id=int(m.group(1)),
                    text=m.group(2).strip(),
                    turn_index=turn_index,
                )
            )

    # ------------------------------------------------------------------
    # Phase 3: 16 rules
    # ------------------------------------------------------------------

    def _rule_01_exactly_one_final(self) -> None:
        """Rule 1: exactly one <final_answer>, located in the last assistant turn."""
        n = len(self.parsed.finals)
        if n == 0:
            self.result.add_error(
                "MISSING_FINAL_ANSWER",
                "Trajectory has no <final_answer> (rule 1)",
            )
            return
        if n > 1:
            self.result.add_error(
                "MULTIPLE_FINAL_ANSWERS",
                f"Trajectory has {n} <final_answer> tags; exactly one is required (rule 1)",
            )
            return
        # Verify the final_answer is in the last assistant turn.
        last_assistant_idx = self._last_assistant_turn_index()
        final_turn_idx = self.parsed.finals[0][0]
        if final_turn_idx != last_assistant_idx:
            self.result.add_error(
                "FINAL_ANSWER_NOT_IN_LAST_TURN",
                f"<final_answer> is in turn {final_turn_idx} but the last assistant turn is {last_assistant_idx} (rule 1)",
                turn_index=final_turn_idx,
            )

    def _rule_02_first_assistant_turn(self) -> None:
        """Rule 2: first assistant turn is either lazy or plan_round_1 + ≥1 route_round_1."""
        first_a_idx = self._first_assistant_turn_index()
        if first_a_idx is None:
            self.result.add_error(
                "NO_ASSISTANT_TURN",
                "Trajectory has no assistant turn (rule 2)",
            )
            return
        first_content = self.messages[first_a_idx]["content"]
        has_plan_1 = bool(re.search(r'<plan round="1">', first_content))
        has_route_1 = bool(re.search(r'<route round="1"', first_content))
        has_final = bool(FINAL_RE.search(first_content))

        if has_final and not has_plan_1 and not has_route_1:
            # Lazy mode — valid.
            self.parsed.is_lazy = True
            return
        if has_plan_1 and has_route_1:
            return
        self.result.add_error(
            "INVALID_FIRST_ASSISTANT_TURN",
            "First assistant turn must be either lazy mode (<final_answer> only) "
            "or contain both <plan round=\"1\"> and at least one <route round=\"1\"> (rule 2)",
            turn_index=first_a_idx,
        )

    def _rule_03_plan_round_monotonic(self) -> None:
        """Rule 3: plan rounds strictly increasing 1, 2, 3, …; round=1 at most once."""
        if not self.parsed.plan_rounds:
            return  # lazy mode is allowed
        rounds = self.parsed.plan_rounds
        # Round 1 at most once
        if rounds.count(1) > 1:
            self.result.add_error(
                "PLAN_ROUND_1_DUPLICATED",
                f"<plan round=\"1\"> appears {rounds.count(1)} times; must appear at most once (rule 3)",
            )
        # Strictly increasing 1, 2, 3, …
        if rounds and rounds[0] != 1:
            self.result.add_error(
                "PLAN_FIRST_ROUND_NOT_1",
                f"First <plan> has round={rounds[0]}; must start at 1 (rule 3)",
            )
        for i in range(1, len(rounds)):
            if rounds[i] != rounds[i - 1] + 1:
                self.result.add_error(
                    "PLAN_ROUND_NOT_CONSECUTIVE",
                    f"<plan> rounds are not strictly consecutive: {rounds} (rule 3)",
                )
                break

    def _rule_04_subtask_id_monotonic(self) -> None:
        """Rule 4: subtask ids globally strictly increasing across all rounds."""
        # Reconstruct the *declaration order* of subtasks across the trajectory.
        ordered_ids: list[int] = []
        for msg in self.messages:
            if msg.get("role") != "assistant":
                continue
            for m in PLAN_RE.finditer(msg.get("content", "")):
                for sm in SUBTASK_RE.finditer(m.group(2)):
                    ordered_ids.append(int(sm.group(1)))
        for i in range(1, len(ordered_ids)):
            if ordered_ids[i] <= ordered_ids[i - 1]:
                self.result.add_error(
                    "SUBTASK_ID_NOT_INCREASING",
                    f"Subtask ids are not strictly increasing across the trajectory: {ordered_ids} (rule 4)",
                )
                break

    def _rule_05_depends_on_validity(self) -> None:
        """Rule 5: depends_on references must be already declared, no cycles, within plan must be earlier."""
        # Build the same declaration order as rule 4.
        decl_order: list[int] = []
        decl_round: dict[int, int] = {}
        for msg in self.messages:
            if msg.get("role") != "assistant":
                continue
            for m in PLAN_RE.finditer(msg.get("content", "")):
                round_num = int(m.group(1))
                for sm in SUBTASK_RE.finditer(m.group(2)):
                    sid = int(sm.group(1))
                    decl_order.append(sid)
                    decl_round[sid] = round_num
        decl_index = {sid: i for i, sid in enumerate(decl_order)}

        for sid, st in self.parsed.subtasks.items():
            for dep in st.depends_on:
                if dep == sid:
                    self.result.add_error(
                        "DEPENDS_ON_SELF",
                        f"Subtask {sid} depends on itself (rule 5)",
                    )
                    continue
                if dep not in self.parsed.subtasks:
                    self.result.add_error(
                        "DEPENDS_ON_UNDECLARED",
                        f"Subtask {sid} depends on undeclared subtask {dep} (rule 5)",
                    )
                    continue
                # Within-plan: dep must be declared earlier in the trajectory.
                if decl_index.get(dep, -1) >= decl_index.get(sid, -1):
                    self.result.add_error(
                        "DEPENDS_ON_NOT_EARLIER",
                        f"Subtask {sid} depends on {dep} which is not declared earlier (rule 5)",
                    )
            # Ascending check (closed-vocab convention §2 / §7).
            if st.depends_on != sorted(st.depends_on):
                self.result.add_error(
                    "DEPENDS_ON_NOT_ASCENDING",
                    f"Subtask {sid} depends_on={st.depends_on} is not ascending (schema §2)",
                )

        # Cycle detection (defensive; the earlier-declared rule should already
        # exclude cycles, but malformed inputs may slip through).
        if not self._is_dag():
            self.result.add_error(
                "DEPENDS_ON_CYCLE",
                "depends_on relations contain a cycle (rule 5)",
            )

    def _rule_06_route_obs_pairing(self) -> None:
        """Rule 6: every routed subtask has exactly one matching obs (by subtask id)."""
        routed_ids = [r.subtask_id for r in self.parsed.routes]
        obs_ids = [o.subtask_id for o in self.parsed.obs_list]

        # Each routed id must appear exactly once in obs.
        for sid in routed_ids:
            count = obs_ids.count(sid)
            if count == 0:
                self.result.add_error(
                    "ROUTED_SUBTASK_MISSING_OBS",
                    f"Subtask {sid} was routed but has no <obs> (rule 6)",
                )
            elif count > 1:
                self.result.add_error(
                    "ROUTED_SUBTASK_DUPLICATE_OBS",
                    f"Subtask {sid} has {count} <obs> tags; must have exactly one (rule 6)",
                )
        # Conversely, every obs must reference a routed subtask.
        for sid in obs_ids:
            if sid not in routed_ids:
                self.result.add_error(
                    "OBS_FOR_UNROUTED_SUBTASK",
                    f"<obs subtask=\"{sid}\"> has no matching <route> (rule 6)",
                )
        # Each route's referenced subtask must actually exist.
        for r in self.parsed.routes:
            if r.subtask_id not in self.parsed.subtasks:
                self.result.add_error(
                    "ROUTE_REFERENCES_UNDECLARED_SUBTASK",
                    f"<route subtask=\"{r.subtask_id}\"> references an undeclared subtask (rule 6)",
                    turn_index=r.turn_index,
                )

    def _rule_07_route_round_matches_plan(self) -> None:
        """Rule 7: route.round must equal the round of the plan containing the referenced subtask."""
        for r in self.parsed.routes:
            st = self.parsed.subtasks.get(r.subtask_id)
            if st is None:
                continue  # already reported by rule 6
            if r.round != st.declared_in_round:
                self.result.add_error(
                    "ROUTE_ROUND_MISMATCH",
                    f"<route round=\"{r.round}\" subtask=\"{r.subtask_id}\"> "
                    f"but subtask was declared in plan round {st.declared_in_round} (rule 7)",
                    turn_index=r.turn_index,
                )

    def _rule_08_verify_position(self) -> None:
        """Rule 8: <verify round=N> must appear immediately after all obs of round N.

        We approximate "immediately after" as: in the message immediately
        following the last tool turn that contains an obs for any subtask of
        round N. This is the most natural translation of the rule and matches
        canonical exemplars.
        """
        for v in self.parsed.verifies:
            # Find subtasks declared in this round.
            round_subtask_ids = {
                sid for sid, st in self.parsed.subtasks.items() if st.declared_in_round == v.round
            }
            if not round_subtask_ids:
                self.result.add_error(
                    "VERIFY_ROUND_HAS_NO_PLAN",
                    f"<verify round=\"{v.round}\"> but no <plan round=\"{v.round}\"> exists (rule 8)",
                    turn_index=v.turn_index,
                )
                continue
            # Find the latest obs turn for any of those subtasks.
            obs_turns = [
                o.turn_index for o in self.parsed.obs_list if o.subtask_id in round_subtask_ids
            ]
            if not obs_turns:
                # No obs at all in this round but verify exists — only valid if
                # the round had zero routes (defensive).
                continue
            latest_obs = max(obs_turns)
            # The verify must be in an assistant turn that comes after latest_obs.
            if v.turn_index <= latest_obs:
                self.result.add_error(
                    "VERIFY_BEFORE_OBS",
                    f"<verify round=\"{v.round}\"> in turn {v.turn_index} but the last obs of round {v.round} is in turn {latest_obs} (rule 8)",
                    turn_index=v.turn_index,
                )

    def _rule_09_verify_pass_followed_by_final(self) -> None:
        """Rule 9: <verify status=pass> must be followed by <final_answer>, no further <plan>."""
        for v in self.parsed.verifies:
            if v.status != "pass":
                continue
            # All later plans must be empty.
            later_plans_exist = any(
                st.declared_in_round > v.round for st in self.parsed.subtasks.values()
            )
            if later_plans_exist:
                self.result.add_error(
                    "VERIFY_PASS_FOLLOWED_BY_PLAN",
                    f"<verify round=\"{v.round}\" status=\"pass\"> is followed by additional plans (rule 9)",
                    turn_index=v.turn_index,
                )
            # final_answer must exist after this verify.
            if not self.parsed.finals:
                continue  # rule 1 will catch this
            final_turn = self.parsed.finals[0][0]
            if final_turn < v.turn_index:
                self.result.add_error(
                    "VERIFY_PASS_BEFORE_FINAL_MISSING",
                    f"<verify round=\"{v.round}\" status=\"pass\"> is in turn {v.turn_index} but no <final_answer> follows (rule 9)",
                    turn_index=v.turn_index,
                )

    def _rule_10_verify_repair_followed_by_plan(self) -> None:
        """Rule 10: <verify status=repair_needed> MUST be followed by <plan round=N+1>, never directly by <final_answer>."""
        for v in self.parsed.verifies:
            if v.status != "repair_needed":
                continue
            next_round = v.round + 1
            has_next_plan = any(r == next_round for r in self.parsed.plan_rounds)
            if not has_next_plan:
                self.result.add_error(
                    "VERIFY_REPAIR_NO_NEXT_PLAN",
                    f"<verify round=\"{v.round}\" status=\"repair_needed\"> is not followed by <plan round=\"{next_round}\"> (rule 10)",
                    turn_index=v.turn_index,
                )

    def _rule_11_verify_target_ascending_declared(self) -> None:
        """Rule 11: verify repair_needed target must be non-empty ascending list of declared subtask ids."""
        for v in self.parsed.verifies:
            if v.status == "pass":
                if v.target:
                    self.result.add_error(
                        "VERIFY_PASS_HAS_TARGET",
                        f"<verify round=\"{v.round}\" status=\"pass\"> must not carry a target attribute (rule 11)",
                        turn_index=v.turn_index,
                    )
                continue
            # status == repair_needed
            if not v.target:
                self.result.add_error(
                    "VERIFY_REPAIR_EMPTY_TARGET",
                    f"<verify round=\"{v.round}\" status=\"repair_needed\"> has no target (rule 11)",
                    turn_index=v.turn_index,
                )
                continue
            if v.target != sorted(v.target):
                self.result.add_error(
                    "VERIFY_TARGET_NOT_ASCENDING",
                    f"<verify round=\"{v.round}\" target=\"{v.target}\"> is not ascending (rule 11)",
                    turn_index=v.turn_index,
                )
            for sid in v.target:
                if sid not in self.parsed.subtasks:
                    self.result.add_error(
                        "VERIFY_TARGET_UNDECLARED",
                        f"<verify round=\"{v.round}\" target> references undeclared subtask {sid} (rule 11)",
                        turn_index=v.turn_index,
                    )

    def _rule_12_closed_vocab_model_skill(self) -> None:
        """Rule 12: route.model in available_models, route.skill in available_skills,
        and (model, skill) pair must be in model.allowed_skills if pools provides it."""
        if self.pools is None:
            if self.parsed.routes:
                self.result.add_warning(
                    "WARN_POOLS_NOT_PROVIDED",
                    f"Closed-vocabulary check skipped: pools=None ({len(self.parsed.routes)} routes unverified)",
                )
            return
        models = set(self.pools.get("available_models", []))
        skills = set(self.pools.get("available_skills", []))
        model_allowed_skills: dict[str, list[str]] = self.pools.get("model_allowed_skills", {})
        deprecated_models: set[str] = self.pools.get("deprecated_models", set())
        policy_model: str | None = self.pools.get("policy_model")
        enforcement: str = self.pools.get("enforcement", "strict")

        for r in self.parsed.routes:
            # ROUTE_MODEL_IS_POLICY: never route to the training backbone itself.
            if policy_model and r.model == policy_model:
                self.result.add_error(
                    "ROUTE_MODEL_IS_POLICY",
                    f"<route subtask=\"{r.subtask_id}\" model=\"{r.model}\"> targets the policy backbone, "
                    f"which is forbidden (rule 12)",
                    turn_index=r.turn_index,
                )
                continue
            if r.model not in models:
                self.result.add_error(
                    "ROUTE_MODEL_NOT_IN_POOL",
                    f"<route subtask=\"{r.subtask_id}\" model=\"{r.model}\"> not in available_models (rule 12)",
                    turn_index=r.turn_index,
                )
                continue
            if r.model in deprecated_models:
                self.result.add_error(
                    "ROUTE_MODEL_DEPRECATED",
                    f"<route subtask=\"{r.subtask_id}\" model=\"{r.model}\"> is deprecated (rule 12)",
                    turn_index=r.turn_index,
                )
            if r.skill not in skills:
                self.result.add_error(
                    "ROUTE_SKILL_NOT_IN_POOL",
                    f"<route subtask=\"{r.subtask_id}\" skill=\"{r.skill}\"> not in available_skills (rule 12)",
                    turn_index=r.turn_index,
                )
                continue
            # Per-model allowed_skills whitelist (only enforced when enforcement=strict).
            if enforcement == "strict":
                allowed = model_allowed_skills.get(r.model)
                if allowed is not None and r.skill not in allowed:
                    self.result.add_error(
                        "MODEL_SKILL_PAIR_NOT_ALLOWED",
                        f"<route subtask=\"{r.subtask_id}\" model=\"{r.model}\" skill=\"{r.skill}\"> "
                        f"is not in model.allowed_skills={allowed} (rule 12, strict mode)",
                        turn_index=r.turn_index,
                    )

    def _rule_13_route_count_cap(self) -> None:
        """Rule 13: total <route> count ≤ 8."""
        n = len(self.parsed.routes)
        if n > MAX_ROUTES:
            self.result.add_error(
                "ROUTE_COUNT_EXCEEDED",
                f"Total routes = {n}, exceeds cap {MAX_ROUTES} (rule 13)",
            )

    def _rule_14_plan_round_count_cap(self) -> None:
        """Rule 14: total <plan> rounds ≤ 3 (initial + ≤ 2 repair)."""
        n = len(self.parsed.plan_rounds)
        if n > MAX_PLAN_ROUNDS:
            self.result.add_error(
                "PLAN_ROUND_COUNT_EXCEEDED",
                f"Total plan rounds = {n}, exceeds cap {MAX_PLAN_ROUNDS} (rule 14)",
            )

    # ------------------------------------------------------------------
    # Phase 4: stats
    # ------------------------------------------------------------------

    def _compute_stats(self) -> None:
        models_used = sorted({r.model for r in self.parsed.routes})
        skills_used = sorted({r.skill for r in self.parsed.routes})
        repair_targets_per_round = {
            v.round: v.target for v in self.parsed.verifies if v.status == "repair_needed"
        }
        n_rounds = len(self.parsed.plan_rounds)
        self.result.stats = {
            "is_lazy": self.parsed.is_lazy,
            "n_plan_rounds": n_rounds,
            "n_repair_rounds": max(0, n_rounds - 1),
            "n_subtasks": len(self.parsed.subtasks),
            "n_routes": len(self.parsed.routes),
            "n_obs": len(self.parsed.obs_list),
            "n_verifies": len(self.parsed.verifies),
            "n_finals": len(self.parsed.finals),
            "models_used": models_used,
            "skills_used": skills_used,
            "repair_targets_per_round": repair_targets_per_round,
            "n_messages": len(self.messages),
            "n_assistant_turns": sum(1 for m in self.messages if m.get("role") == "assistant"),
            "n_tool_turns": sum(1 for m in self.messages if m.get("role") == "tool"),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _first_assistant_turn_index(self) -> int | None:
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "assistant":
                return i
        return None

    def _last_assistant_turn_index(self) -> int | None:
        last = None
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "assistant":
                last = i
        return last

    def _is_dag(self) -> bool:
        # Kahn's algorithm.
        nodes = list(self.parsed.subtasks.keys())
        in_degree = {n: 0 for n in nodes}
        adj: dict[int, list[int]] = {n: [] for n in nodes}
        for sid, st in self.parsed.subtasks.items():
            for dep in st.depends_on:
                if dep in adj:
                    adj[dep].append(sid)
                    in_degree[sid] += 1
        queue = [n for n in nodes if in_degree[n] == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for nxt in adj[node]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        return visited == len(nodes)


# ---------------------------------------------------------------------------
# Self-test (run with: python scripts/validate_schema.py)
# ---------------------------------------------------------------------------


def _self_test() -> None:
    """Smoke test using the three canonical exemplars from schema §6.

    Loads the real config/pools.yaml so this test also exercises the YAML
    loader and the per-model allowed_skills whitelist (rule 12 strict mode).
    """
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    pools_path = os.path.normpath(os.path.join(here, "..", "config", "pools.yaml"))
    if os.path.exists(pools_path):
        pools = load_pools(pools_path)
        print(f"loaded pools from {pools_path}: "
              f"{len(pools['available_models'])} models, "
              f"{len(pools['available_skills'])} skills")
    else:
        # Fallback inline pool for environments without the file (CI / fresh checkout).
        pools = {
            "available_models": [
                "claude-haiku-4-5-20251001",
                "claude-sonnet-4-6",
                "claude-opus-4-6",
                "gpt-5.1-low",
                "gpt-5.4",
                "gpt-5.3-codex",
                "gemini-2.5-flash",
                "gemini-3-pro-preview",
                "kimi-k2.5",
                "qwen3-max",
                "qwen3-coder-plus",
            ],
            "available_skills": ["direct_solve", "retrieval", "code_exec", "math_calc"],
            "model_allowed_skills": {
                "claude-haiku-4-5-20251001": ["direct_solve", "retrieval", "math_calc"],
                "claude-sonnet-4-6": ["direct_solve", "retrieval", "code_exec", "math_calc"],
                "gpt-5.3-codex": ["direct_solve", "code_exec"],
            },
            "deprecated_models": set(),
        }

    # Exemplar 1: lazy
    lazy = [
        {"role": "system", "content": "You are a hierarchical agent."},
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "<final_answer>Paris</final_answer>"},
    ]
    r = validate_messages(lazy, pools=pools)
    assert r.valid, f"Exemplar 1 (lazy) should be valid; errors={r.errors}"
    assert r.stats["is_lazy"] is True
    assert r.stats["n_routes"] == 0

    # Exemplar 2: one-shot success
    oneshot = [
        {"role": "system", "content": "You are a hierarchical agent."},
        {
            "role": "user",
            "content": "Compute the GDP per capita of the country whose capital is Lima.",
        },
        {
            "role": "assistant",
            "content": (
                '<plan round="1">\n'
                '  <subtask id="1" depends_on="">Identify the country whose capital is Lima</subtask>\n'
                '  <subtask id="2" depends_on="1">Find that country\'s 2023 GDP</subtask>\n'
                '  <subtask id="3" depends_on="1">Find that country\'s 2023 population</subtask>\n'
                '  <subtask id="4" depends_on="2,3">Compute GDP per capita</subtask>\n'
                '</plan>\n'
                '<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="direct_answer">Country whose capital is Lima</route>\n'
                '<route round="1" subtask="2" model="claude-haiku-4-5-20251001" skill="web_search">Peru 2023 nominal GDP</route>\n'
                '<route round="1" subtask="3" model="claude-haiku-4-5-20251001" skill="web_search">Peru 2023 population</route>\n'
                '<route round="1" subtask="4" model="gpt-5.3-codex" skill="execute_python">Compute GDP per capita</route>'
            ),
        },
        {
            "role": "tool",
            "content": (
                '<obs subtask="1">Peru.</obs>\n'
                '<obs subtask="2">Peru 2023 nominal GDP: $267.6B USD.</obs>\n'
                '<obs subtask="3">Peru 2023 population: 34.35 million.</obs>\n'
                '<obs subtask="4">7790.39 USD.</obs>'
            ),
        },
        {
            "role": "assistant",
            "content": (
                '<verify round="1" status="pass">All four subtasks succeeded.</verify>\n'
                '<final_answer>Approximately $7,790 USD per capita.</final_answer>'
            ),
        },
    ]
    r = validate_messages(oneshot, pools=pools)
    assert r.valid, f"Exemplar 2 (one-shot) should be valid; errors={r.errors}"
    assert r.stats["n_routes"] == 4
    assert r.stats["n_subtasks"] == 4
    assert r.stats["n_plan_rounds"] == 1

    # Exemplar 3: repair loop
    repair = [
        {"role": "system", "content": "You are a hierarchical agent."},
        {"role": "user", "content": "Some query needing X and Y."},
        {
            "role": "assistant",
            "content": (
                '<plan round="1">\n'
                '  <subtask id="1" depends_on="">Find X</subtask>\n'
                '  <subtask id="2" depends_on="">Find Y</subtask>\n'
                '</plan>\n'
                '<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="web_search">query for X</route>\n'
                '<route round="1" subtask="2" model="claude-haiku-4-5-20251001" skill="web_search">query for Y</route>'
            ),
        },
        {
            "role": "tool",
            "content": (
                '<obs subtask="1">X = ...</obs>\n'
                '<obs subtask="2">No relevant results found.</obs>'
            ),
        },
        {
            "role": "assistant",
            "content": (
                '<verify round="1" status="repair_needed" target="2">Subtask 2 retrieval failed.</verify>\n'
                '<plan round="2">\n'
                '  <subtask id="3" depends_on="">Broader retrieval for Y</subtask>\n'
                '</plan>\n'
                '<route round="2" subtask="3" model="gemini-3.1-pro-preview" skill="web_search">broader query for Y</route>'
            ),
        },
        {"role": "tool", "content": '<obs subtask="3">Y = ...</obs>'},
        {
            "role": "assistant",
            "content": (
                '<verify round="2" status="pass">Recovered evidence.</verify>\n'
                '<final_answer>...</final_answer>'
            ),
        },
    ]
    r = validate_messages(repair, pools=pools)
    assert r.valid, f"Exemplar 3 (repair) should be valid; errors={r.errors}"
    assert r.stats["n_plan_rounds"] == 2
    assert r.stats["n_repair_rounds"] == 1
    assert r.stats["n_routes"] == 3

    # Negative test: missing final
    bad = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": '<plan round="1"><subtask id="1" depends_on="">x</subtask></plan><route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="web_search">x</route>'},
        {"role": "tool", "content": '<obs subtask="1">x</obs>'},
        {"role": "assistant", "content": '<verify round="1" status="pass">ok</verify>'},
    ]
    r = validate_messages(bad, pools=pools)
    assert not r.valid, "Trajectory missing <final_answer> should fail"
    assert any(e.code == "MISSING_FINAL_ANSWER" for e in r.errors)

    # Negative test: repair without next plan
    bad2 = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": '<plan round="1"><subtask id="1" depends_on="">x</subtask></plan><route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="web_search">x</route>'},
        {"role": "tool", "content": '<obs subtask="1">x</obs>'},
        {"role": "assistant", "content": '<verify round="1" status="repair_needed" target="1">bad</verify><final_answer>guess</final_answer>'},
    ]
    r = validate_messages(bad2, pools=pools)
    assert not r.valid, "repair_needed followed directly by final_answer should fail"
    assert any(e.code == "VERIFY_REPAIR_NO_NEXT_PLAN" for e in r.errors)

    # Negative test: bad model name
    bad3 = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": '<plan round="1"><subtask id="1" depends_on="">x</subtask></plan><route round="1" subtask="1" model="gpt-9000" skill="web_search">x</route>'},
        {"role": "tool", "content": '<obs subtask="1">x</obs>'},
        {"role": "assistant", "content": '<verify round="1" status="pass">ok</verify><final_answer>x</final_answer>'},
    ]
    r = validate_messages(bad3, pools=pools)
    assert not r.valid, "Out-of-vocab model should fail"
    assert any(e.code == "ROUTE_MODEL_NOT_IN_POOL" for e in r.errors)

    # Negative test: model+skill pair not allowed (codex × retrieval)
    bad4 = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": '<plan round="1"><subtask id="1" depends_on="">x</subtask></plan><route round="1" subtask="1" model="gpt-5.3-codex" skill="web_search">x</route>'},
        {"role": "tool", "content": '<obs subtask="1">x</obs>'},
        {"role": "assistant", "content": '<verify round="1" status="pass">ok</verify><final_answer>x</final_answer>'},
    ]
    r = validate_messages(bad4, pools=pools)
    assert not r.valid, "codex × retrieval should fail (not in allowed_skills)"
    assert any(e.code == "MODEL_SKILL_PAIR_NOT_ALLOWED" for e in r.errors), \
        f"Expected MODEL_SKILL_PAIR_NOT_ALLOWED, got {[e.code for e in r.errors]}"

    # Negative test: routing to the policy backbone itself
    if pools.get("policy_model"):
        bad5 = [
            {"role": "system", "content": "x"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": (
                '<plan round="1"><subtask id="1" depends_on="">x</subtask></plan>'
                f'<route round="1" subtask="1" model="{pools["policy_model"]}" skill="direct_answer">x</route>'
            )},
            {"role": "tool", "content": '<obs subtask="1">x</obs>'},
            {"role": "assistant", "content": '<verify round="1" status="pass">ok</verify><final_answer>x</final_answer>'},
        ]
        r = validate_messages(bad5, pools=pools)
        assert not r.valid, "Routing to policy backbone should fail"
        assert any(e.code == "ROUTE_MODEL_IS_POLICY" for e in r.errors), \
            f"Expected ROUTE_MODEL_IS_POLICY, got {[e.code for e in r.errors]}"

    print("validate_schema self-test: ALL PASS")


if __name__ == "__main__":
    _self_test()
