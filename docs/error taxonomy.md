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

The router probe is `pass@3`: each of the 12,803 sampled tasks is attempted three times, and the task counts as solved only if *any* attempt passes the per-source verifier. 5,589 tasks (43.7%) are solved and discarded; the remaining 7,214 tasks yield 21,642 failing rollouts that we classify below. The router sees the same source-aware planner prompt the teacher uses (including the ToolACE tool-schema injection), so the failures reported here reflect capability gaps, not prompting choices.

The router's pass@3 success rate varies sharply across capability axes:

| Capability Axis         | Router Success Rate |
| ----------------------- | ------------------: |
| Atomic reasoning        |               96.6% |
| Knowledge retrieval     |               64.8% |
| Compositional reasoning |               66.4% |
| Knowledge composition   |               42.3% |
| Tool orchestration      |               14.3% |

Tool orchestration is the deepest capability gap: the router solves only 14.3% of TACO+ToolACE tasks under pass@3, mirroring the motivation for learned routing — the router cannot yet match coding sub-tasks to code-generation specialists or function-calling sub-tasks to tool-aware workers.

We classify the 21,642 failure rollouts by root cause:

| Root Cause                  |  Count | Share | Description                                                              |
| --------------------------- | -----: | ----: | ------------------------------------------------------------------------ |
| Output not code             |  7,417 | 34.2% | Returned numeric, natural-language, or skeleton output instead of code   |
| Wrong entity                |  4,887 | 22.6% | Retrieved or reasoned to an incorrect entity (QA tasks)                  |
| No finish / incomplete      |  2,932 | 13.5% | Trajectory terminated without a `finish()` call                          |
| No tool call                |  1,230 |  5.7% | ToolACE rollout answered without issuing any tool call                   |
| Numeric reasoning error     |  1,221 |  5.6% | Incorrect arithmetic over passages or math prompts                       |
| Format/reasoning error      |    746 |  3.4% | Mathematically inequivalent answer, wrong interval, wrong MC letter      |
| Partial QA overlap          |    508 |  2.3% | Answer overlaps with gold but verifier rejects it                        |
| NL instead of API call      |    454 |  2.1% | Prose description instead of a structured function call                  |
| Loop / stall                |    376 |  1.7% | Repeated identical tool calls until the step budget runs out             |
| Empty answer                |    351 |  1.6% | `finish("")` with an empty payload                                       |
| Close numeric miss          |    400 |  1.8% | Within 10% of gold but not accepted                                      |
| Wrong code logic            |     94 |  0.4% | Structurally complete code with an incorrect algorithm                   |
| Other                       |  1,426 |  6.6% | Refusals, context hints, wrong API function, rounding, etc.              |

The two dominant failure modes — *output not code* (34.2%) and *wrong entity* (22.6%) — together account for 57% of failing rollouts. Both are *delegation failures*: the router either sends a competitive-programming task to a model that summarizes in prose or handles multi-hop QA without routing to search-capable workers. *No finish / incomplete* and *no tool call* (19.2% combined) are protocol failures concentrated almost entirely on tool orchestration.

### Per-Source Breakdown

#### GSM8K — 51 failure rollouts (Router success 96.6%)

| Root Cause          | Count | Share |
| ------------------- | ----: | ----: |
| `calculation_error` |    33 | 64.7% |
| `rounding_precision` |   15 | 29.4% |
| `off_by_10x`        |     3 |  5.9% |

Pure arithmetic capability limitation. The router correctly identifies these single-step tasks as not requiring decomposition (95%+ success).

#### NuminaMath — 1,806 failure rollouts (Router success 66.4%)

| Root Cause                  | Count | Share |
| --------------------------- | ----: | ----: |
| `wrong_answer_calculation`  |   778 | 43.1% |
| `wrong_answer_math_form`    |   639 | 35.4% |
| `wrong_answer_choice_letter`|   107 |  5.9% |
| `wrong_answer_rounding`     |   100 |  5.5% |
| `wrong_answer_off_by_2x`    |    82 |  4.5% |
| `wrong_answer_zero`         |    34 |  1.9% |
| Other                       |    66 |  3.7% |

Competition-level mathematics where the router produces a mathematically inequivalent answer (different interval notation, unsimplified fractions, wrong MC letter) or a plain calculation error. Format mismatches (~35%) can be closed with better normalization; the remainder requires capability.

#### DROP — 1,650 failure rollouts (Router success 69.4%)

| Root Cause                  | Count | Share |
| --------------------------- | ----: | ----: |
| `wrong_answer_wrong_entity` |   768 | 46.5% |
| `wrong_answer_numeric_far`  |   635 | 38.5% |
| `empty_answer`              |   126 |  7.6% |
| `wrong_answer_numeric_close`|    78 |  4.7% |
| `wrong_answer_partial_overlap` | 26 |  1.6% |
| Other                       |    17 |  1.0% |

Balanced between entity extraction errors and numeric reasoning over passages.

#### HotpotQA — 2,376 failure rollouts (Router success 60.6%)

| Root Cause                     | Count | Share |
| ------------------------------ | ----: | ----: |
| `wrong_answer_wrong_entity`    | 1,785 | 75.1% |
| `wrong_answer_partial_overlap` |   222 |  9.3% |
| `empty_answer`                 |   193 |  8.1% |
| `wrong_answer_numeric_close`   |    91 |  3.8% |
| `wrong_answer_numeric_far`     |    61 |  2.6% |
| Other                          |    24 |  1.0% |

Overwhelmingly wrong-entity errors. The 2-hop structure means a wrong first hop cascades into a wrong second hop.

#### MuSiQue — 3,021 failure rollouts (Router success 42.3%)

| Root Cause                     | Count | Share |
| ------------------------------ | ----: | ----: |
| `wrong_answer_wrong_entity`    | 2,334 | 77.3% |
| `wrong_answer_partial_overlap` |   260 |  8.6% |
| `wrong_answer_numeric_close`   |   231 |  7.6% |
| `wrong_answer_numeric_far`     |   125 |  4.1% |
| Other                          |    71 |  2.4% |

Hardest QA source. The 3–4 hop structure compounds wrong-entity rates multiplicatively; the router has no mechanism to verify intermediate hops before routing the next sub-query.

#### TACO — 8,013 failure rollouts (Router success 15.4%)

| Root Cause                         | Count | Share |
| ---------------------------------- | ----: | ----: |
| `wrong_answer_numeric_not_code`    | 4,121 | 51.4% |
| `wrong_answer_not_code`            | 3,296 | 41.1% |
| `no_finish_or_incomplete`          |   119 |  1.5% |
| `loop_or_stall`                    |   205 |  2.6% |
| `wrong_answer_trivial_code`        |   135 |  1.7% |
| `wrong_answer_code_logic`          |    94 |  1.2% |
| Other                              |    43 |  0.5% |

93% of failures produce non-code output: the router answers in prose or a plain number rather than delegating to a code-generation specialist. Only 1.2% are genuine algorithmic failures — pure delegation-strategy gap.

#### ToolACE — 4,725 failure rollouts (Router success 12.5%)

| Root Cause                          | Count | Share |
| ----------------------------------- | ----: | ----: |
| `no_finish_or_incomplete`           | 2,774 | 58.7% |
| `no_tool_call_in_answer`            | 1,230 | 26.0% |
| `wrong_answer_nl_instead_of_tool`   |   454 |  9.6% |
| `loop_or_stall`                     |   154 |  3.3% |
| `wrong_tool_completely`             |    35 |  0.7% |
| `refusal_cannot_execute`            |    19 |  0.4% |
| Other                               |    59 |  1.3% |

84% of failures are protocol violations — the router either issues no tool call or never reaches a `finish()` within the step budget. Another 10% issue a natural-language description of the desired call instead of the structured call itself. These patterns are what SFT should close first, before any routing-quality optimization is meaningful on this source.

---

## Qwen3-4B vs Qwen2.5-7B Router Comparison

We evaluate a smaller router (Qwen3-4B-Instruct) on the same curriculum. Its failure profile differs qualitatively:

| Failure Mode                         | Qwen2.5-7B | Qwen3-4B |
| ------------------------------------ | ---------: | -------: |
| Protocol failure (no finish / empty) |     22.8% |    98.1% |
| Wrong-answer content error           |     76.9% |     1.9% |
| Missing context / refusal            |      0.2% |      0.0% |

The 7B router's failures are dominated by *capability* limitations (wrong answers), while the 4B router fails almost exclusively at *protocol compliance* (unable to produce valid tool calls or a terminal finish action). This suggests that protocol-following ability is a prerequisite that emerges between 4B and 7B scale, and that SFT for the 4B model should prioritize format compliance before routing quality — whereas SFT for the 7B model can target content-level delegation decisions directly.
