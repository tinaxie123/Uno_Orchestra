#!/usr/bin/env bash
# Minimal non-Docker smoke evaluation. Requires LOCAL_BASE/API_BASE to be up.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

API_KEY="${API_KEY:-EMPTY}"
API_BASE="${API_BASE:-http://localhost:9000/v1}"
LOCAL_BASE="${LOCAL_BASE:-http://localhost:8000/v1}"
LOCAL_MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
OUT="${EVAL_OUT:-data/eval}/smoke"

python scripts/check_eval_env.py

python -m eval_pipeline \
  --router planner \
  --bench gpqa \
  --api_key "${API_KEY}" \
  --api_base "${API_BASE}" \
  --local_base "${LOCAL_BASE}" \
  --local_model "${LOCAL_MODEL}" \
  --output_dir "${OUT}/planner_gpqa" \
  --max_tasks 1 \
  --pass-k 1 \
  --gen_workers 1 \
  --verify_workers 1

python scripts/collect_results.py --root "${OUT}" --format md
