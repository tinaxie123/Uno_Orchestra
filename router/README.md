# Learned Selective Delegation Router

Multi-agent orchestration via learned routing. The router decomposes questions into subtasks and routes each to the best (model, skill) pair.

## Structure

```
router/
  pipeline.md                 # Complete data generation & training pipeline
  experiment_plan.md       # Experiment design (locked)
  experiment_guide.md
  config/
    pools.yaml                # 9 models, 13 skills
    sft_recipe.yaml           # 31 datasets, 10 domains, 41.5k targets
  scripts/
    generate_trajectories.py                # Teacher distillation → trajectory JSONL
    validate_schema.py       # Schema validation (16 rules)
    build_dataset.py        # JSONL → training parquet
    audit_quality.py              # Dataset quality audit
  data/
    trajectory_schema.md            # Trajectory schema specification
    checklist.md    # Pre-distillation gates
    train_final_stats.json    # Final dataset statistics (58,457 samples)
```

## Quick Start

See [pipeline.md](pipeline.md) for the complete pipeline from distillation to training.

## Dataset

58,457 validated trajectories across 9 domains. Schema, 100% pass rate. Training data at server `/home/xieht/data/sft/train_final.parquet`.
