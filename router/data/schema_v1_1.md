# Trajectory Schema v1.1 (LOCKED)

**Status**: LOCKED — single source of truth for SFT data, RL data, distillation prompts, parser, env implementation, and the paper's Method section.

**Scope**: Single-model hierarchical agent doing single-shot decomposition with iterative repair, trained with SFT warm-start + GiGPO RL.

**Compatibility**: ChatML multi-turn, verl-agent `MultiTurnSFTDataset`, parquet storage.

---

## 1. Design summary

- **One model** (Qwen2.5-7B-Instruct), no classification heads, no separate verifier.
- **One SFT pass** on multi-turn ChatML data; loss masked by role (assistant only).
- **Single-shot decomposition with iterative repair**: Leader emits a structured plan in one shot, Router assigns capabilities per subtask, Verifier checks the result, optional repair rounds (≤ 2).
- **Two policies in the credit-assignment sense, one network in the parameter sense.**
- **Capability routing, not agent orchestration**: every routed call is a stateless capability invocation; `direct_solve` is a first-class skill.

---

## 2. Tag definitions

| Tag | Role | Required attrs | Optional attrs | Semantics |
|---|---|---|---|---|
| `<plan round="N">…</plan>` | assistant | `round` | — | Container for one decomposition round |
| `<subtask id="K" depends_on="…">…</subtask>` | assistant (inside `<plan>`) | `id`, `depends_on` | — | One node in the task DAG |
| `<route round="N" subtask="K" model="…" skill="…">…</route>` | assistant | `round`, `subtask`, `model`, `skill` | — | Capability assignment for one subtask (Router decision anchor) |
| `<obs subtask="K">…</obs>` | tool | `subtask` | — | Environment feedback for one subtask |
| `<verify round="N" status="…" target="…">…</verify>` | assistant | `round`, `status` | `target` (only when `status="repair_needed"`) | Verifier judgement |
| `<final_answer>…</final_answer>` | assistant | — | — | Termination |

### Removed in v1.1
- ❌ `<think>`: not in v1.1. Distillation must not produce reasoning traces. A future v1.2 may reintroduce a short `<reasoning>` (≤ 30 words) for a high-quality subset only.

### Closed-vocabulary attributes
- `model` ∈ `available_models` (declared in `config/pools.yaml`).
- `skill` ∈ `available_skills` (declared in `config/pools.yaml`); v1.1 set is `{direct_solve, retrieval, code_exec, math_calc}`.
- `model` and `skill` MUST be string literals from these closed sets. Free-form values are invalid samples and are dropped by the parser.

### Attribute conventions
- `depends_on`: **always present**. No dependencies → `depends_on=""`. Multiple dependencies → comma-separated, ascending: `depends_on="1,2"` (not `"2,1"`).
- `subtask.id`: globally strictly increasing across all rounds; ids are never reused.
- `route.round`: must equal the `round` of the `<plan>` containing the referenced `<subtask>`.
- `verify.target`: only present when `status="repair_needed"`; ascending comma-separated list of subtask ids; absent when `status="pass"`.
- `verify.status`: strict enum `{"pass", "repair_needed"}`.

### `skill="direct_solve"`
- Means: invoke the chosen `model` with no external tool. The model answers from its parametric knowledge alone.
- This is the **collapse action**: routing a subtask to `direct_solve` is the policy declaring "no tool needed for this subtask".
- It is distinct from emitting `<final_answer>` directly without any `<plan>` (the *lazy mode*, which collapses the whole question to zero decomposition).

---

## 3. Validity constraints (parser-enforced; violating samples are discarded)

1. Exactly one `<final_answer>`, located in the last assistant turn.
2. The first assistant turn satisfies one of:
   - **Lazy mode**: `<final_answer>` directly, with no `<plan>` and no `<route>`; **or**
   - **Plan mode**: a `<plan round="1">` containing ≥ 1 `<subtask>`, followed by ≥ 1 `<route round="1">`.
3. `<plan round="N">` rounds are strictly increasing 1, 2, 3, …; `round="1"` appears at most once.
4. `<subtask id="K">` ids are globally strictly increasing across the entire trajectory; ids are never reused across rounds.
5. **Dependency declaration**: every id referenced in `depends_on` MUST be declared somewhere in the current trajectory, MUST NOT form a cycle, and within each `<plan>` referenced dependencies MUST be declared earlier in the same plan or in previous rounds.
6. Every `<subtask>` that has been routed MUST receive exactly one `<obs>` with the matching `subtask` id.
7. `<route>`'s `round` attribute MUST equal the `round` of the `<plan>` containing the referenced `<subtask>`.
8. `<verify round="N">` MUST appear immediately after all `<obs>` of round N.
9. `<verify status="pass">` MUST be followed by `<final_answer>` (no further `<plan>`).
10. **`<verify status="repair_needed">` MUST be followed by `<plan round="N+1">`**. It is *not* allowed to be followed directly by `<final_answer>` — repair_needed implies another decomposition round. (A future "best_effort" status may be introduced if a giving-up branch is needed; v1.1 does not allow it.)
11. `<verify status="repair_needed">`'s `target` MUST be a non-empty ascending comma-separated list of already-declared `<subtask>` ids.
12. `route.model` ∈ `available_models`; `route.skill` ∈ `available_skills`.
13. Total `<route>` count across all rounds ≤ 8.
14. Total `<plan>` round count ≤ 3 (initial + at most 2 repair rounds).
15. `<obs>` MAY appear only in `role: "tool"` turns.
16. `<plan>`, `<subtask>`, `<route>`, `<verify>`, `<final_answer>` MAY appear only in `role: "assistant"` turns.

---

## 4. Loss-mask regions (SFT)

verl `MultiTurnSFTDataset` masks by message role automatically.

- **Loss = 1** (assistant turns, all content): `<plan>`, `<subtask>`, `<route>`, `<verify>`, `<final_answer>`.
- **Loss = 0** (tool turns and system/user turns): `<obs>`, system prompt, user query.

No additional masking configuration is required.

---

## 5. RL credit-assignment regions (GiGPO)

| Span type | Includes | Advantage source |
|---|---|---|
| **Leader span** | `<plan>` (with all `<subtask>` children); `<final_answer>` | Episode-level group baseline (across N rollouts of the same query) |
| **Router span** | each `<route>` (a subtask-level routing decision **anchor**) | Step-level group baseline, constructed across alternative route realizations sampled at the same query × same round × same subtask slot |
| **Verify span** | each `<verify>` | Independent advantage; if final answer is wrong but `verify.status="pass"`, the verify span receives a dedicated negative advantage that does not pollute Leader/Router spans |

### Method-section phrasing (use verbatim)
> Each `<route>` span serves as the **anchor** for a subtask-level routing decision. Step-level grouping is constructed across alternative route realizations sampled at the same query, same round, and same subtask slot, rather than across different subtasks within a single plan.

### Implementation note
At rollout time, in addition to the N episode-level rollouts, for each `<subtask>` we additionally sample K alternative `<route>` realizations (keeping the plan fixed). These K samples form the step-level group for that subtask slot. Default: N=8, K=4.

---

## 6. Canonical exemplars

These exemplars are the reference samples used in the Method section figures and in the distillation prompt as in-context examples.

### Exemplar 1 — Lazy (zero decomposition)

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "What is the capital of France?"},
    {"role": "assistant", "content": "<final_answer>Paris</final_answer>"},
]
```
Demonstrates: full collapse to `<final_answer>` with no `<plan>` and no `<route>`.

### Exemplar 2 — One-shot success (plan + parallel route + verify pass)

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content":
        "Compute the GDP per capita of the country whose capital is Lima, "
        "using its 2023 GDP and population."},

    {"role": "assistant", "content":
        '<plan round="1">\n'
        '  <subtask id="1" depends_on="">Identify the country whose capital is Lima</subtask>\n'
        '  <subtask id="2" depends_on="1">Find that country\'s 2023 GDP</subtask>\n'
        '  <subtask id="3" depends_on="1">Find that country\'s 2023 population</subtask>\n'
        '  <subtask id="4" depends_on="2,3">Compute GDP per capita</subtask>\n'
        '</plan>\n'
        '<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="direct_solve">Country whose capital is Lima</route>\n'
        '<route round="1" subtask="2" model="claude-haiku-4-5-20251001" skill="retrieval">Peru 2023 nominal GDP</route>\n'
        '<route round="1" subtask="3" model="claude-haiku-4-5-20251001" skill="retrieval">Peru 2023 population</route>\n'
        '<route round="1" subtask="4" model="gpt-5.1-codex" skill="code_exec">Compute GDP per capita: GDP / population</route>'
    },

    {"role": "tool", "content":
        '<obs subtask="1">Peru.</obs>\n'
        '<obs subtask="2">Peru 2023 nominal GDP: $267.6 billion USD.</obs>\n'
        '<obs subtask="3">Peru 2023 population: 34.35 million.</obs>\n'
        '<obs subtask="4">267600000000 / 34350000 = 7790.39 USD.</obs>'
    },

    {"role": "assistant", "content":
        '<verify round="1" status="pass">All four subtasks succeeded; values are consistent and the computation is sound.</verify>\n'
        '<final_answer>Approximately $7,790 USD per capita (Peru, 2023).</final_answer>'
    },
]
```
Demonstrates: dependency-aware DAG, mixed `direct_solve` / `retrieval` / `code_exec`, dependency-respecting execution, single-round verify pass.

### Exemplar 3 — Repair loop

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "<some query requiring two facts X and Y>"},

    {"role": "assistant", "content":
        '<plan round="1">\n'
        '  <subtask id="1" depends_on="">Find X</subtask>\n'
        '  <subtask id="2" depends_on="">Find Y</subtask>\n'
        '</plan>\n'
        '<route round="1" subtask="1" model="claude-haiku-4-5-20251001" skill="retrieval">query for X</route>\n'
        '<route round="1" subtask="2" model="claude-haiku-4-5-20251001" skill="retrieval">query for Y</route>'
    },

    {"role": "tool", "content":
        '<obs subtask="1">X = ...</obs>\n'
        '<obs subtask="2">No relevant results found.</obs>'
    },

    {"role": "assistant", "content":
        '<verify round="1" status="repair_needed" target="2">Subtask 2 retrieval returned no relevant results; need a broader query and a stronger retriever model.</verify>\n'
        '<plan round="2">\n'
        '  <subtask id="3" depends_on="">Broader retrieval for Y with alternative phrasing</subtask>\n'
        '</plan>\n'
        '<route round="2" subtask="3" model="claude-sonnet-4-6" skill="retrieval">broader query for Y</route>'
    },

    {"role": "tool", "content":
        '<obs subtask="3">Y = ...</obs>'
    },

    {"role": "assistant", "content":
        '<verify round="2" status="pass">Subtask 3 successfully recovered the missing evidence; combined results are now sufficient.</verify>\n'
        '<final_answer>...</final_answer>'
    },
]
```
Demonstrates: verify-triggered repair, tier escalation (haiku → sonnet) on retry, monotonic id sequence across rounds, ≤ 2 repair rounds.

---

## 7. Distillation prompt requirements

The distillation prompt MUST enforce all of the following on the teacher model output. Samples violating any rule are discarded by the parser before being written to the SFT parquet.

1. `depends_on` is always present, including `depends_on=""`.
2. `route.round` always equals the `round` of its containing `<plan>`.
3. `verify.status` is an attribute, never embedded in free text.
4. `verify status="repair_needed"` always carries a `target` attribute with ascending ids.
5. `verify status="repair_needed"` is always followed by another `<plan>`, never by `<final_answer>`.
6. No `<think>`, `<reasoning>`, or chain-of-thought in any form.
7. **Lazy mode coverage**: ≥ 30% of distilled samples should be lazy (`<final_answer>` directly) or single-route to ensure the policy can collapse fully.
8. **Repair coverage**: ≥ 30% of distilled samples should contain at least one repair round to give RL exploration prior over the repair branch.
9. `model` and `skill` MUST be exact strings from the closed vocabularies in `config/pools.yaml`.
10. `target`, `depends_on`, and any other multi-id list are written in **ascending order**.

---

## 8. Parser regex (reference implementation)

```python
import re

PLAN_RE    = re.compile(r'<plan round="(\d+)">(.*?)</plan>', re.DOTALL)
SUBTASK_RE = re.compile(r'<subtask id="(\d+)" depends_on="([^"]*)">(.*?)</subtask>', re.DOTALL)
ROUTE_RE   = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
OBS_RE     = re.compile(r'<obs subtask="(\d+)">(.*?)</obs>', re.DOTALL)
VERIFY_RE  = re.compile(
    r'<verify round="(\d+)" status="(pass|repair_needed)"(?: target="([^"]*)")?>(.*?)</verify>',
    re.DOTALL,
)
FINAL_RE   = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)
```

The parser uses these regexes to (a) verify all validity constraints in §3, (b) extract span boundaries for RL credit assignment per §5, and (c) compute the analysis metadata listed in §10.

---

## 9. Storage layout

### SFT parquet — `data/sft/sft_warmstart.parquet`

Columns:

| Column | Type | Description |
|---|---|---|
| `messages` | list[dict] | ChatML; this is the **only column read by `MultiTurnSFTDataset`** |
| `id` | str | Unique sample id, e.g. `hotpotqa_train_5e4f3a` |
| `data_source` | str | HF dataset name |
| `domain` | str | One of the 10 SFT recipe domains |
| `difficulty` | str | `easy` / `medium` / `hard` |
| `n_subtasks_planned` | int | Total `<subtask>` count |
| `n_routes` | int | Total `<route>` count |
| `n_rounds` | int | Total `<plan>` rounds |
| `n_repair_rounds` | int | `n_rounds − 1` |
| `is_lazy` | bool | True iff trajectory has zero `<route>` |
| `models_used` | list[str] | Distinct `model` values across all `<route>` |
| `skills_used` | list[str] | Distinct `skill` values across all `<route>` |
| `ground_truth` | str | Verifier reference answer |
| `distilled_by` | str | Teacher model id |
| `format_valid` | bool | Always True (invalid samples are not written) |
| `answer_verified` | bool | Whether the final answer matches gold via the source's verifier |
| `input_tokens` | int | Teacher input token count |
| `output_tokens` | int | Teacher output token count |

### RL parquet — `data/rl/rl_pool.parquet`

Same `id` keying as the SFT parquet (one-to-one with the SFT samples). Columns mirror verl-agent's expected format (`data_source`, `prompt`, `ability`, `reward_model`, `extra_info`); see `data/rl_format.md` for details. The `prompt` column contains only system + user messages; trajectories are generated by the env at rollout time, not stored.

---

## 10. Lock status

Schema **v1.1 LOCKED** as of this commit. All downstream artifacts (`config/pools.yaml`, `config/sft_recipe.yaml`, `scripts/distill.py`, the env implementation, and the paper's Method section) MUST conform to this document. Any change requires bumping to v1.2 and updating all dependents.

### Changelog from earlier drafts
- v0 → v1.0: introduced `<plan>`, `<route>`, `<obs>`, `<verify>`, `<final_answer>`, `round`, explicit `depends_on`, single-shot decomposition + repair.
- v1.0 → v1.1: (1) `skill="none"` → `skill="direct_solve"`; (2) `verify status="repair_needed"` MUST be followed by `<plan>`, never directly by `<final_answer>`; (3) dependency-declaration constraint reworded to reference declaration order, not temporal order; (4) `model`/`skill` declared as closed vocabularies; (5) `target` and `depends_on` lists declared ascending; (6) `<think>` removed.
