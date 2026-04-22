# Error Taxonomy & Data Statistics

## Curriculum Filtering Pipeline

We sample 12,803 tasks across five capability axes from seven public benchmarks. The planner prompt is source-aware: for function-calling benchmarks (ToolACE) we inject the dataset's native tool-schema into the planner system message; for all other sources the prompt is uniform. Each task passes through a three-stage curriculum filter:

1. **Router probe** (pass\@3): the current policy router (Qwen2.5-7B-Instruct) attempts the task three times. If any attempt succeeds, the task is *discarded* as already solved — it carries no learning signal for the router.
2. **Teacher trajectory**: a strong teacher model (Qwen3.5-Plus) solves the remaining tasks via the full planner → router → executor pipeline. Successful trajectories enter the **SFT candidate pool**; failed trajectories enter the **RL pool** with sparse outcome reward.
3. **Overlong filtering** (SFT only): trajectories whose token count exceeds the training context length (8,192 tokens) are discarded. Truncated trajectories would teach the model to produce incomplete decompositions.

Of the 12,803 sampled tasks, 5,589 (43.7%) are already solved by the current router and discarded. Of the 7,214 tasks that survive to the teacher stage, 3,174 yield successful teacher trajectories and 4,549 fail; after overlong filtering the final **SFT set contains 2,762 trajectories** and the **RL pool contains 4,549 tasks**.

**Table 1.** Data distribution by capability axis.

| Capability Axis         | Benchmarks     |    Sampled |       Router OK |   SFT | RL Pool | SFT Share |
| ----------------------- | -------------- | ---------: | --------------: | ----: | ------: | --------: |
| Atomic reasoning        | GSM8K          |        500 |     483 (96.6%) |    40 |      19 |      1.4% |
| Compositional reasoning | NuminaMath     |      1,793 |   1,191 (66.4%) |   278 |     511 |     10.1% |
| Knowledge retrieval     | DROP, HotpotQA |      3,808 |   2,466 (64.8%) |   528 |     963 |     19.1% |
| Knowledge composition   | MuSiQue        |      1,746 |     739 (42.3%) |   182 |   1,007 |      6.6% |
| Tool orchestration      | TACO, ToolACE  |      4,956 |     710 (14.3%) | 1,734 |   2,049 |     62.8% |
| **Total**               |                | **12,803** | **5,589 (43.7%)** | **2,762** | **4,549** | |

Tool orchestration receives the largest share of SFT demonstrations (62.8%) because routing decisions in this axis involve both model selection *and* skill selection. Atomic reasoning receives the smallest share (1.4%) because the router already solves 96.6% of these tasks; the remaining SFT examples serve primarily as a negative signal, teaching the router to recognize single-step tasks that should *not* be decomposed. The near-zero router-OK rate for tool orchestration (14.3%) validates our design choice to treat tool selection as a learned routing problem rather than a fixed heuristic.

---

## Qwen2.5-7B Router Failure Taxonomy

To characterize *where the student is weak* — the gaps that SFT and RL must close — we classify every failed router rollout on the 7,214 tasks the Qwen2.5-7B router does not solve under pass\@3. The router sees the same source-aware planner prompt that the teacher uses (including the ToolACE tool-schema injection), so the failures reported here reflect capability gaps, not prompting choices. Roughly three-quarters of these failures are content errors (wrong answers), and the remaining quarter are protocol errors (no-finish, no tool call, loops). The content failures concentrate on three delegation patterns: **wrong-entity errors on multi-hop QA** (HotpotQA, MuSiQue), where the router cannot maintain coherent reasoning chains across hops; **non-code output on competitive programming** (TACO), where the router answers in prose despite having access to a code-generation specialist in its pool; and **natural-language answers on function-calling tasks** (ToolACE), where the router produces an English description of an API call instead of issuing the call — even though the tool schema is present in its context. The protocol failures are heavily skewed toward tool use: more than half of ToolACE rollouts terminate without emitting any tool call, and TACO trajectories frequently time out before producing a finish. These patterns are consistent with a 7B model that can *reason* locally but has not yet learned the *shape* of a multi-step delegation or the *routing policy* that selects the right specialist for each sub-task: SFT teaches the shape from teacher demonstrations, and RL on the 4,549-task residual pool optimizes the content-level routing decisions that remain after the shape is fixed.

---

## Qwen3-4B vs Qwen2.5-7B Router Comparison

We evaluate a smaller router (Qwen3-4B-Instruct) on the same curriculum. Its failure profile differs qualitatively:

| Failure Mode                         | Qwen2.5-7B | Qwen3-4B |
| ------------------------------------ | ---------: | -------: |
| Protocol failure (no finish / empty) |     22.8% |    98.1% |
| Wrong-answer content error           |     76.9% |     1.9% |
| Missing context / refusal            |      0.2% |      0.0% |

The 7B router's failures are dominated by *capability* limitations (wrong answers), while the 4B router fails almost exclusively at *protocol compliance* (unable to produce valid tool calls or a terminal finish action). This suggests that protocol-following ability is a prerequisite that emerges between 4B and 7B scale, and that SFT for the 4B model should prioritize format compliance before routing quality — whereas SFT for the 7B model can target content-level delegation decisions directly.
