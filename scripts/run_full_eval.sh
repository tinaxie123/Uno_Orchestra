#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Full evaluation matrix: all models × all benchmarks
# Uses the real Planner → Router → Worker framework for +/+claude variants
#
# Usage:
#   bash scripts/run_full_eval.sh                    # run all
#   bash scripts/run_full_eval.sh --bench gpqa,mmlu  # run specific benchmarks
#   bash scripts/run_full_eval.sh --dry-run           # print commands only
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ──
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi
EVAL_OUT="${EVAL_OUT:-${PROJECT_DIR}/data/eval}"
API_KEY="${API_KEY:-EMPTY}"
API_BASE="${API_BASE:-http://localhost:9000/v1}"      # API model gateway (9-model pool)
LOCAL_BASE="${LOCAL_BASE:-http://localhost:8000/v1}"   # vLLM local model (planner/router)
GEN_WORKERS="${GEN_WORKERS:-8}"
VERIFY_WORKERS="${VERIFY_WORKERS:-4}"
PASS_K="${PASS_K:-2}"

# ── Paper benchmark suite (13 benchmarks) ──
ALL_BENCHMARKS=(gpqa mmlu math500 aime drop humaneval mbpp gaia livecodebench toolbench mrcr swebench terminalbench)

# ── Models to evaluate ──
# Format: "NAME|ROUTER_TYPE|LOCAL_MODEL|EXTRA_ARGS"
#
# ROUTER_TYPE:
#   direct  = model answers directly, no routing (baseline)
#   random  = randomly pick from model pool (baseline)
#   planner = paper-style unified Uno policy router (schema + route harness)
#
MODELS=(
    # ━━━ Direct baselines (model answers directly, no decomposition/routing) ━━━
    "RouterRL_Qwen25-7B_Direct|direct|Qwen/Qwen2.5-7B-Instruct|"
    "RouterRL_Qwen3-4B_Direct|direct|Qwen/Qwen3-4B|"

    # ━━━ Random routing baselines ━━━
    "RouterRL_Qwen25-7B_Random|random|Qwen/Qwen2.5-7B-Instruct|"
    "RouterRL_Qwen3-4B_Random|random|Qwen/Qwen3-4B|"

    # ━━━ Full Uno-Orchestra policy router (full model/skill pool) ━━━
    "RouterRL_Qwen25-7B_Plus|planner|Qwen/Qwen2.5-7B-Instruct|"
    "RouterRL_Qwen3-4B_Plus|planner|Qwen/Qwen3-4B|"

    # ━━━ Pre-trained router models (update checkpoint paths) ━━━
    # "Router-R1|planner|/path/to/router-r1-checkpoint|"
    # "Wideseek-R1|planner|/path/to/wideseek-r1-checkpoint|"
)

# ── Parse args ──
DRY_RUN=false
SELECTED_BENCHMARKS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --bench)
            IFS=',' read -ra SELECTED_BENCHMARKS <<< "$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Unknown arg: $1"
            exit 1
            ;;
    esac
done

if [[ ${#SELECTED_BENCHMARKS[@]} -eq 0 ]]; then
    SELECTED_BENCHMARKS=("${ALL_BENCHMARKS[@]}")
fi

# ── Run evaluation matrix ──
echo "════════════════════════════════════════════════════════════"
echo "  Evaluation Matrix"
echo "  Benchmarks: ${SELECTED_BENCHMARKS[*]}"
echo "  Models: ${#MODELS[@]} configurations"
echo "  Output: ${EVAL_OUT}"
echo ""
echo "  Pipeline:"
echo "    direct  → model answers question directly"
echo "    random  → randomly route to API model pool"
echo "    planner → Uno policy emits <route model skill> → Worker API/harness"
echo ""
echo "  Scoring:"
echo "    official_compatible → benchmark-standard verifier flow"
echo "    uno_harness         → multi-turn Uno harness score"
echo "════════════════════════════════════════════════════════════"

TOTAL=0
DONE=0

for model_spec in "${MODELS[@]}"; do
    IFS='|' read -r MODEL_NAME ROUTER LOCAL_MODEL EXTRA <<< "$model_spec"

    for BENCH in "${SELECTED_BENCHMARKS[@]}"; do
        TOTAL=$((TOTAL + 1))
        OUT_DIR="${EVAL_OUT}/${MODEL_NAME}/${BENCH}"
        BENCH_EXTRA=""
        if [[ "${BENCH}" == "swebench" || "${BENCH}" == "terminalbench" ]]; then
            BENCH_EXTRA="--interactive"
        fi

        # Skip if already completed
        if [[ -f "${OUT_DIR}/summary.json" ]]; then
            echo "[SKIP] ${MODEL_NAME} × ${BENCH} (already done)"
            DONE=$((DONE + 1))
            continue
        fi

        mkdir -p "$(dirname "${OUT_DIR}.log")"

        CMD="python -m eval_pipeline \
            --router ${ROUTER} \
            --bench ${BENCH} \
            --api_key ${API_KEY} \
            --api_base ${API_BASE} \
            --local_base ${LOCAL_BASE} \
            --local_model ${LOCAL_MODEL} \
            --output_dir ${OUT_DIR} \
            --gen_workers ${GEN_WORKERS} \
            --verify_workers ${VERIFY_WORKERS} \
            --pass-k ${PASS_K} \
            ${BENCH_EXTRA} \
            ${EXTRA}"

        if $DRY_RUN; then
            echo "[DRY] ${MODEL_NAME} × ${BENCH}:"
            echo "      ${CMD}"
        else
            echo ""
            echo "──────────────────────────────────────────────"
            echo "[RUN] ${MODEL_NAME} × ${BENCH} (router=${ROUTER})"
            echo "──────────────────────────────────────────────"
            cd "${PROJECT_DIR}"
            eval "${CMD}" 2>&1 | tee "${OUT_DIR}.log" || {
                echo "[FAIL] ${MODEL_NAME} × ${BENCH}"
                continue
            }
            DONE=$((DONE + 1))
        fi
    done
done

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Done: ${DONE}/${TOTAL} evaluations"
echo "  Results: ${EVAL_OUT}/"
echo "  Collect: python scripts/collect_results.py --root ${EVAL_OUT} --format md"
echo "════════════════════════════════════════════════════════════"
