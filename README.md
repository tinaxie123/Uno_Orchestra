# Selective Delegation: When Does Multi-Agent Help?

Controlled empirical study showing delegation benefit is concentrated and predictable.

## Structure

```
oracle_analysis/       # Core experiment: oracle protocol, pipelines, analysis
  config.example.py    # Copy to config.py, fill API keys
  run_oracle.py        # Main: runs direct/tool/delegate × K seeds
  pipelines.py         # 3 pipelines + ablation (equal budget)
  evaluate.py          # LLM judge + oracle analysis
  annotate.py          # Pre-registered structural annotation
  analyze.py           # Concentration + boundary analysis
  tools.py             # Web search, file read, calculator, Python exec

baselines/
  AOrchestra/          # Sub-agent orchestration baseline (always delegate)
  MARTI/               # Multi-agent RL framework

experiment_plan.md   #  experiment plan
```

## Quick Start

```bash
# 1. Set up oracle analysis
cd oracle_analysis
cp config.example.py config.py
# Edit config.py with your API keys

# 2. Run oracle analysis on GAIA
python run_oracle.py --dataset gaia --max-queries 50 --K 3

# 3. Run analysis
python analyze.py results/gaia_oracle.jsonl
```

## Baselines

| Baseline   | Source                            | Requirements   |
| ---------- | --------------------------------- | -------------- |
| AOrchestra | baselines/AOrchestra/             | API mode       |
| Router-R1  | github.com/ulab-uiuc/Router-R1    | gpu(3B router) |
| AFlow      | github.com/FoundationAgents/AFlow | -              |

