# Case Study — Subtask-to-Subtask Conflict (`hyperbola`)

A NuminaMath trajectory that showcases how the Planner detects **constraint
conflict between its own subtasks** and spawns a meta-subtask to resolve the
inconsistency. This is the `verify → repair` pattern in action on an
over-determined mathematical problem.

## Task

> Find the standard equation of a hyperbola $C_2$ that:
> - passes through $A(2, -\frac{\sqrt{2}}{2})$ and $B(-\sqrt{2}, -\frac{\sqrt{3}}{2})$,
> - shares asymptotes with $C_1: \frac{x^2}{5} - \frac{y^2}{3} = 1$,
> - has focal length 8.

(Source: NuminaMath. Gold answer: $\frac{y^2}{6} - \frac{x^2}{10} = 1$.)

The three constraints are **jointly inconsistent** — no hyperbola can
simultaneously pass through both $A$ and $B$ while sharing the asymptotes of
$C_1$ at focal length 8. A good Planner should detect this and pick the
*intended* answer by prioritising the clean geometric constraints (asymptotes
+ focal length) over the specific points.

## Planner decomposition (3 subtasks)

| Subtask | Router pick `(model, skill)` | Role |
|---|---|---|
| `t1` | `gemini-3-flash-preview / symbolic_math` | Attempt the direct solve (enumerate $k > 0$ / $k < 0$) |
| `t2` | `gemini-2.5-flash-lite / direct_answer` | Re-analyze from scratch in case of arithmetic error |
| `t3` | `gemini-3-flash-preview / symbolic_math` | **Meta-subtask**: given the inconsistency, pick the intended answer |

The key line from `t3`'s instruction:

> *"Given the apparent inconsistency in the problem, determine the most likely
> intended answer."*

And `t3`'s response:

> *"Based on your analysis of the constraints, you've correctly identified the
> core conflict: the points $A$ and $B$ are mathematically incompatible with a
> hyperbola that shares asymptotes with $C_1$ and has a focal length of 8.
> In competitive mathematics where constraints are over-determined and
> inconsistent, the standard 'intended' path usually prioritizes the geometric
> properties (asymptotes and focal length) over specific coordinate
> passages."*

## Why this matters

This is the exact pattern we want trained planners to reproduce:

1. **Attempt**: delegate a direct-solve subtask (`t1`).
2. **Re-attempt independently**: delegate a second subtask with fresh
   re-analysis (`t2`) — a lightweight cross-check; if both fail the same way
   the inconsistency is real, not a worker error.
3. **Verify conflict**: the Planner recognises from `t1` + `t2` results that
   the two points give incompatible $k$.
4. **Meta-repair**: delegate a *new* subtask (`t3`) whose job is to *resolve
   the conflict* — "given both branches fail, which answer does the problem
   most likely intend?".

`t3` is **not a repeat of `t1`/`t2`**; it takes the output of the previous
two subtasks as context and reasons about the problem's meta-structure.
This corresponds to the `<verify ... status="repair_needed"> → <plan round=2>`
pattern in our SFT training schema.

## Teacher's final answer vs gold

- Teacher chose the $k > 0$ branch: $\frac{x^2}{10} - \frac{y^2}{6} = 1$.
- Gold answer uses the $k < 0$ branch: $\frac{y^2}{6} - \frac{x^2}{10} = 1$.

So even though the Planner correctly *recognised* the conflict and made a
principled choice, the teacher picked the wrong branch of the two consistent
geometric answers. This specific trajectory therefore enters the RL pool (not
SFT). The value it contributes to the pool is the *conflict-recognition
pattern* — not the final numeric answer.

## Contrast with `fix-git`

- In [`fix-git`](../fix-git/README.md), the Planner produced 3 subtasks but
  only 1 touched state; there was no verify subtask, so the merge-conflict
  resolution error by the worker went undetected → reward 0.
- Here, the Planner **did** issue a verify-like second subtask (`t2`) and a
  meta-repair subtask (`t3`). The conflict got recognised. The only miss was
  the final branch selection.

Putting the two side by side:

| Aspect | `fix-git` | `hyperbola` |
|---|---|---|
| Planner decomposition | Thin (1 of 3 real) | Rich (3 of 3 meaningful) |
| Verify step present? | ❌ | ✅ (t2 cross-check, t3 meta-reason) |
| Repair on detection? | ❌ | ✅ (t3 picks intended answer) |
| Task solved? | ❌ (silent worker error) | ❌ (wrong branch picked) |
| Failure mode | undetected semantic error | branch-ambiguity at final |

Both fail, but the `hyperbola` case illustrates the orchestration pattern we
want the trained router to reproduce: detect → cross-check → resolve.

## Artefact

- [`trajectory.json`](./trajectory.json) — raw subtask instructions and
  worker responses.
