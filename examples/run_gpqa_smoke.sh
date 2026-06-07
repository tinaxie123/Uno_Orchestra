#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

python -m eval_pipeline \
  --router planner \
  --bench gpqa \
  --api_key "${API_KEY:-EMPTY}" \
  --api_base "${API_BASE:-http://localhost:9000/v1}" \
  --local_base "${LOCAL_BASE:-http://localhost:8000/v1}" \
  --local_model "${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct}" \
  --output_dir data/eval/examples/gpqa_smoke \
  --max_tasks 1 \
  --pass-k 1 \
  --gen_workers 1 \
  --verify_workers 1

python scripts/collect_results.py --root data/eval/examples --format md
