# Learned Selective Delegation Router

Multi-agent orchestration via learned routing. The router decomposes questions into subtasks and routes each to the best (model, skill) pair.

## Structure

```
router/
  pipeline.md                    # Complete data generation & training pipeline
  experiment_plan.md             # Experiment design (benchmark split, eval plan)
  config/
    pools.yaml                   # 9 models, 13 skills
    sft_recipe.yaml              # 31 datasets, 10 domains
  scripts/
    generate_trajectories.py     # Teacher distillation → trajectory JSONL
    validate_schema.py           # Trajectory schema validation (16 rules)
    build_dataset.py             # JSONL → training parquet
  data/
    trajectory_schema.md         # Trajectory format specification
```

## Quick Start

See [pipeline.md](pipeline.md) for the complete pipeline from distillation to training.

## Dataset

58,457 validated trajectories across 9 domains, schema 100% pass rate.
