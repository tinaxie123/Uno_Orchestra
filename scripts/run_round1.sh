#!/bin/bash
# Round 1: Full bootstrapped curriculum filtering (~10k tasks)
# Pipeline: Router probe (pass@3) → Teacher trajectory → Overlong filter → SFT/RL split
set -euo pipefail

PYTHON=/home/xieht/.conda/envs/marl/bin/python
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Planner + Router: local Qwen 2.5 7B
PLANNER_MODEL="Qwen/Qwen2.5-7B-Instruct"
PLANNER_API_BASE="http://localhost:8234/v1"
ROUTER_MODEL="Qwen/Qwen2.5-7B-Instruct"
ROUTER_API_BASE="http://localhost:8234/v1"

SUB_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
SUB_API_KEY="sk-70793c3ec75a40ca90b7076de9927260"
TEACHER_MODEL="qwen3.5-plus"
TEACHER_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
TEACHER_API_KEY="sk-70793c3ec75a40ca90b7076de9927260"
OUT_DIR="${REPO_ROOT}/data/sft/round1"
CONCURRENCY=32
SEED=42

echo "=== Round 1: Bootstrapped Curriculum Filtering ==="
echo "Planner/Router: ${PLANNER_MODEL} @ ${PLANNER_API_BASE}"
echo "Teacher:        ${TEACHER_MODEL} @ ${TEACHER_API_BASE}"
echo "Concurrency:    ${CONCURRENCY}"
echo "Output:         ${OUT_DIR}"
echo ""

$PYTHON "${REPO_ROOT}/scripts/data/generate_trajectories.py" \
    --planner-model "${PLANNER_MODEL}" \
    --planner-api-base "${PLANNER_API_BASE}" \
    --planner-api-key "none" \
    --router-model "${ROUTER_MODEL}" \
    --router-api-base "${ROUTER_API_BASE}" \
    --router-api-key "none" \
    --teacher-model "${TEACHER_MODEL}" \
    --teacher-api-base "${TEACHER_API_BASE}" \
    --teacher-api-key "${TEACHER_API_KEY}" \
    --sub-model-api-base "${SUB_API_BASE}" \
    --sub-model-api-key "${SUB_API_KEY}" \
    --out-dir "${OUT_DIR}" \
    --concurrency "${CONCURRENCY}" \
    --seed "${SEED}"
