# Delegation Necessity Is Concentrated and Predictable

## Positioning

This is a finding-first paper, not a router-first paper.

The paper does **not** lead with:
- "we train a better router"

The paper instead leads with:
- "under equal budget, delegation is useful only in a concentrated region of task space"
- "this region is predictable from a very small number of interpretable task-structure variables"
- "a simple router can exploit this boundary and recover most of the oracle utility"

The router is the practical payoff of the finding, not the scientific centerpiece.

## One-Line Claim

Under equal compute budget, delegation is oracle-optimal only for a concentrated subset of queries, and this subset is predictable from a small number of interpretable task-structure variables; a simple learned router can exploit this boundary and recover most of the oracle utility at substantially lower cost than always delegating.

## Core Design Principles

- Action space is predefined: `direct`, `tool_agent`, `delegate`
- Decision boundary is not predefined; it is discovered from oracle data
- All comparisons are made under controlled, equal-budget conditions
- The main claim must survive held-out confirmation
- The main text uses only a minimal number of interpretable variables

## Contributions

1. We introduce a controlled oracle protocol that compares `direct`, `tool_agent`, and `delegate` under the same model, token budget, tool budget, and backend, isolating structural value from raw compute advantage.
2. We show that delegation benefit is concentrated rather than uniform, and that oracle-optimal delegation occupies a small, predictable region of task space.
3. We validate the practical value of this finding with simple routing policies that approach oracle utility at much lower cost than always-delegate baselines.

## Paper Narrative

### Section 1: Introduction

Current multi-agent work usually assumes delegation is broadly helpful. What is missing is a controlled measurement of **when** delegation helps under matched budget, and whether that boundary is stable enough to learn.

### Section 2: Setup

Define three execution modes:
- `direct`: one agent, no tools
- `tool_agent`: one agent with tools
- `delegate`: one lead agent plus sub-agents with isolated contexts

The action space is fixed. The optimal routing boundary is discovered rather than prescribed.

### Section 3: Controlled Oracle Protocol

Present the equal-budget protocol and the expected-utility oracle.

Main output:
- query-level utility for each action
- oracle-best action as a function of `lambda`

### Section 4: Delegation Benefit Is Concentrated

Show that delegation is not uniformly useful:
- oracle-best action distribution
- delegate-hurt rate
- concentration curve

This section establishes the phenomenon.

### Section 5: Delegation Boundary Discovery

Use a minimal set of interpretable variables to characterize where delegation is oracle-best.

Main output:
- shallow interpretable models
- 2D phase diagram
- no feature soup

### Section 6: Held-Out Confirmation

Test whether the same boundary reproduces on held-out data not used for discovery.

This section is critical. Without it, the boundary looks post-hoc.

### Section 7: Learned Router

Show that a simple router can exploit the discovered boundary:
- decision tree
- cost-sensitive classifier
- optional contextual bandit

RL is optional and belongs in appendix unless it materially changes the picture.

### Section 8: Ablations and Limits

Show that the effect is not explained away by:
- extra budget
- extra reasoning steps
- trivial implementation asymmetry

## Experimental Protocol

### Action Space

- `direct`
- `tool_agent`
- `delegate`

No other action is needed in the main text.

### Budget Control

For each query, all pipelines share:
- the same base model
- the same token budget `B`
- the same tool-call budget `T`
- the same search backend
- the same retrieval cache policy
- the same stop conditions

Example protocol:

```text
Token budget B = 8192
Tool budget T = 5
Model = Qwen3-8B
Search backend = fixed shared backend

direct:      1 agent, 0 tools, total budget B
tool_agent:  1 agent, at most T tools, total budget B
delegate:    1 lead + sub-agents, at most T tools total, total budget B
```

Delegate must **not** receive extra budget in the main comparison.

### Oracle Utility

For each query `q`, action `a`, and cost tradeoff `lambda`:

```text
U(q, a, lambda) = E[Acc(q, a)] - lambda * E[Cost(q, a)]
```

Where the expectation is estimated with `K = 3-5` independent runs per `(query, action)` pair.

Oracle action:

```text
a*(q, lambda) = argmax_a U(q, a, lambda)
```

This is preferred over single-run majority vote.

## Benchmarks

### Discovery Benchmarks

- `GAIA`
  Agentic tasks with tools and files
- `HotpotQA`
  Dependent multi-hop reasoning

### Confirmation Benchmark

- `FRAMES`
  Held out from discovery; used only to test whether the discovered pattern reproduces

### Appendix Benchmark

- `MMLU-Pro`
  Used only as a sanity check for low-tool, low-delegation settings

Do not let MMLU-Pro carry the main claim.

## Main Experiments

### Table 1: Oracle Analysis

Per benchmark, report:
- `direct` accuracy and cost
- `tool_agent` accuracy and cost
- `delegate` accuracy and cost
- oracle accuracy and utility
- oracle-best action distribution
- delegate-hurt rate

This is the main empirical anchor.

### Figure 1: Oracle Routing Heatmap

Rows:
- benchmark

Columns:
- `lambda` values

Cells:
- proportion of queries where oracle chooses `direct`, `tool_agent`, or `delegate`

This figure shows that routing preference is not constant across task families or cost regimes.

### Figure 2: Delegation Gain Concentration Curve

For each query, sort by:

```text
delegation_gain(q) = U(q, delegate, lambda) - max(U(q, direct, lambda), U(q, tool_agent, lambda))
```

Plot:
- x-axis: fraction of queries
- y-axis: cumulative fraction of total delegation benefit

This figure should visually establish concentration.

## Boundary Discovery

### Minimal Variable Set

Use only two core variables in the main text:
- `external_information_demand`
- `subproblem_separability`

Optional third variable for appendix:
- `context_pressure`

Do not include a long list of loosely defined variables in the main paper.

### Discovery Procedure

On the discovery set only:
- annotate candidate variables
- fit a shallow tree, logistic regression, or GAM
- keep only the smallest stable variable set that explains oracle-best delegation

The goal is not maximal predictive performance. The goal is a reproducible, interpretable boundary.

### Figure 3: Delegation Phase Diagram

Plot:
- x-axis: external information demand
- y-axis: subproblem separability
- color: probability that `delegate` is oracle-best

This is the most important interpretation figure.

## Held-Out Confirmation

### Confirmation Rule

The boundary is discovered on `GAIA + HotpotQA` and then evaluated unchanged on `FRAMES`.

Confirmation questions:
- Does concentration still hold?
- Does the same low-dimensional boundary still track oracle-best delegation?

### Figure 4: Held-Out Reproduction

Side-by-side:
- discovery-set phase diagram
- held-out phase diagram

If the pattern reproduces, the claim is much stronger and much closer to oral quality.

## Learned Router

### Why It Exists

The router is included to show that the discovered boundary is:
- learnable
- stable
- deployable

It is not the main scientific contribution.

### Training Hierarchy

Main text:
1. shallow decision tree
2. cost-sensitive classifier
3. optional contextual bandit

Appendix:
4. GRPO or PPO, only if it materially reduces gap-to-oracle

### Table 2: Router vs Oracle

Report:
- Always Direct
- Always Tool
- Always Delegate
- Decision Tree
- Cost-Sensitive Classifier
- Contextual Bandit
- Oracle

Metrics:
- accuracy
- cost
- utility
- gap-to-oracle

The ideal result is that very simple policies already recover most oracle benefit.

### Figure 5: Pareto Curve

Sweep `lambda` and train routers with cost-sensitive objectives.

Plot:
- x-axis: cost
- y-axis: accuracy or utility

Compare against:
- Always Direct
- Always Delegate
- simple router
- oracle frontier

## Required Controls

### Control A: Equal Budget

Compare:
- `tool_agent(B, T)`
- `delegate(B, T)`

Question:
- does delegation help at the same budget?

### Control B: Extra Budget

Compare:
- `tool_agent(2B, 2T)`
- `delegate(B, T)`

Question:
- is delegation merely standing in for more compute?

### Control C: Longer Single-Agent Reasoning

Compare:
- `tool_agent_longer(B, T)` with similar total reasoning steps
- `delegate(B, T)`

Question:
- is the gain caused by isolation, or simply by more steps?

Optional appendix control:
- context reset single-agent variant

## What Must Hold for Oral-Level Interest

Two conditions must both hold:

1. `delegate` is oracle-best in a clearly concentrated subset, not a vague 40/30/30 distribution
2. the concentrated region is captured by a very small, reproducible decision boundary that survives held-out confirmation

If only the first holds, the paper is likely a strong empirical accept.
If both hold cleanly, the paper enters oral discussion territory.

## What To Avoid

- making GRPO the story
- adding many feature-engineering variables
- mixing too many benchmarks in the main claim
- giving delegate extra compute in the main comparison
- claiming a structural law without held-out confirmation
- turning the paper into a benchmark dump

## Minimal Deliverable for a Strong Submission

If time is limited, prioritize:

1. controlled oracle protocol
2. oracle analysis on GAIA and HotpotQA
3. concentration curve
4. held-out confirmation on FRAMES
5. simple router vs oracle gap
6. equal-budget and longer-single-agent controls

Everything else is secondary.

## Suggested Writing Style

Use restrained claims.

Prefer:
- "concentrated"
- "predictable from a small set of variables"
- "recovers most oracle benefit"
- "substantially reduces cost relative to always-delegate"

Avoid:
- "universal law"
- "oracle-level"
- "solves delegation"
- "fully explains multi-agent systems"

## Execution Timeline

```text
Day 1-2:   smoke test on GAIA, verify budget control and logging
Day 3-7:   full oracle runs on GAIA + HotpotQA
Day 8-9:   concentration analysis + discovery modeling
Day 10-11: held-out confirmation on FRAMES
Day 12-13: equal-budget and longer-single-agent controls
Day 14-15: train tree / classifier / bandit router
Day 16:    generate figures and tables
Day 17-20: write paper
```

## Final Summary

The clean high-upside version of this paper is:

- not a router paper
- not an RL paper
- not a benchmark dump

It is a controlled empirical study showing that delegation is not broadly useful, but instead becomes optimal only in a small and reproducible region of task space, and that this region is simple enough for lightweight routing policies to exploit.
