# Learned Selective Delegation for Multi-Agent Systems

## Research Background

Large language models are increasingly deployed as agents that decompose complex tasks into subtasks and delegate them to specialized workers — a paradigm known as multi-agent orchestration. Recent systems such as AutoGen, CrewAI, and OpenAI Codex subagents demonstrate strong performance on multi-step reasoning tasks by chaining multiple LLM calls.

However, a critical but under-studied question is: **when should an orchestrator delegate, and when should it solve directly?** Current multi-agent frameworks apply a fixed decomposition strategy — either always decompose (incurring unnecessary cost and latency) or never decompose (losing the benefit of specialization). This "always-on" orchestration leads to two failure modes: (1) over-delegation on simple tasks that a single model can handle, wasting compute on redundant sub-agent calls; (2) under-delegation on complex tasks where the orchestrator attempts to solve everything alone and fails.

Existing approaches lack a principled mechanism to learn the delegation boundary from data. Rule-based routing (e.g., keyword matching, difficulty heuristics) is brittle across domains, while end-to-end multi-agent training (e.g., reinforcement learning over full agent graphs) is sample-inefficient and hard to scale. **What is needed is a lightweight, trainable router that learns selective delegation — deciding per-query whether to solve directly or decompose-and-route to specialized workers.**

## Our Method

To address this challenge, we propose a **learned selective delegation router** that trains a compact orchestrator (7B parameters) to make per-query binary decisions: solve directly or delegate once to a (model, skill) pair. The router is trained via supervised fine-tuning on distilled multi-agent trajectories, followed by reinforcement learning with a cost-aware reward signal. Our design includes:

1. **Trajectory Schema**: A structured format (`<plan>` → `<route>` → `<obs>` → `<verify>` → `<final_answer>`) that captures four delegation behaviors — lazy (direct answer), one-shot decomposition, observation-driven continuation, and decomposition repair. The schema enforces closed-vocabulary model/skill routing and dependency-aware subtask ordering.

2. **Evidence-Grounded Distillation**: A teacher distillation pipeline that generates 58,457 training trajectories across 9 domains (31 datasets). For datasets with available context (Wikipedia passages, search results, step-by-step solutions), real evidence is injected into the teacher prompt so that observations contain factual content rather than hallucinations.

3. **Selective Delegation Training**: SFT warm-start teaches the router the trajectory format and delegation patterns. Subsequent PPO fine-tuning with reward = correctness - lambda * cost explicitly optimizes the delegation boundary — the router learns to avoid unnecessary sub-agent calls on easy tasks while still decomposing hard ones.

4. **Depth-1 Constraint**: Unlike recursive multi-agent trees, our router operates at depth=1 only (single delegation step), keeping the research question clean: "is introducing a sub-agent worth it for this query?" This controlled setup enables rigorous ablation of delegation benefit vs. cost.

## Outputs / Current Progress

**Phase C (Data Generation): COMPLETE**
- 58,457 validated SFT trajectories (schema 100% pass, gold match 86%)
- 9/10 domain coverage: multihop QA (33k), STEM (6.6k), single-hop (5.9k), math (5k), commonsense (4.2k), formal logic (1.7k), code (1.1k), long-context (872), domain knowledge (5)
- Teacher models: claude-sonnet-4-6 (69%), claude-opus-4-6 (18%), qwen-max (13%)
- Evidence injection for 20+ datasets with real context fields
- Training data deployed to server (8x H100 80GB)

**Phase D (SFT Training): NEXT**
- Base model: Qwen2.5-7B-Instruct
- Config: 3 epochs, lr=2e-5, effective batch size 128, DeepSpeed ZeRO-2
- Expected: ~2-3 hours on 8x H100

**Phase E (Evaluation): PLANNED**
- Main benchmarks: GAIA, BrowseComp-Plus (held out from training)
- Secondary benchmarks: HotpotQA-dev, GSM8K-test, HumanEval, MMLU-test
- All eval benchmarks strictly excluded from training data

## Repository Structure

```
router/
  README.md                      # Router project overview
  pipeline.md                    # Complete data & training pipeline
  experiment_plan.md             # Experiment design
  experiment_guide.md            # Explained guide
  config/
    pools.yaml                   # 9 models, 13 skills
    sft_recipe.yaml              # 31 datasets, 10 domains
  scripts/
    generate_trajectories.py     # Teacher distillation
    validate_schema.py           # Trajectory schema validation
    build_dataset.py             # Build training parquet
  data/
    trajectory_schema.md         # Trajectory format specification
```
