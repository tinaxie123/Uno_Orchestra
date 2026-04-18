"""
Pre-SFT instruct model baseline evaluation via OpenCompass.

Benchmarks: GPQA Diamond · MATH-500 · AIME 2025 · DROP · HumanEval · MBPP · MMLU (57 subsets)
Models:     Qwen2.5-7B-Instruct · Qwen3-4B

Usage (from repo root):
    bash eval_pipeline/examples/run_base_eval.sh          # launch on GPUs 5,6,7
    bash eval_pipeline/examples/run_base_eval.sh --status  # check progress

The actual OpenCompass config lives at:
    /data/xieht/opencompass/opencompass/configs/eval_marl_base.py

To add a new model, append to `models` list in that config.
To add a new benchmark, add a `with read_base()` import or inline dataset dict.
"""
