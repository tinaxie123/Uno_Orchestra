# Examples

This directory contains small, copyable commands for exercising the evaluation
framework.

## One-Task GPQA Smoke Evaluation

1. Start `LOCAL_BASE` and `API_BASE`.
2. Copy `.env.example` to `.env` and edit endpoint values.
3. Run:

```bash
bash examples/run_gpqa_smoke.sh
```

The script evaluates one GPQA sample with the Uno router and writes results to
`data/eval/examples/gpqa_smoke`.
