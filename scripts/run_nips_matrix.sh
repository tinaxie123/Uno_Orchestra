#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# NIPS experimental matrix: Leading-Agent + Router + multi-turn Docker SubAgent
# Runs all (backbone × worker_mode × router_llm) cells × TB v2 × pass@k.
#
# Cells:
#   (backbone Q25-7B | Q3-4B) × {Direct, Random, +claude}          =  6
#   (backbone Q25-7B | Q3-4B) × + × {vanilla, SFT, RL} router-LLM  =  6
#                                                                  = 12 cells × TB
#
# Usage:
#   bash scripts/run_nips_matrix.sh --smoke           # 5 tasks, pass@1, 2 cells
#   bash scripts/run_nips_matrix.sh                   # full: 89 tasks, pass@3
#   bash scripts/run_nips_matrix.sh --cells identity-q25,learned-sft-q25
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_ROOT="${OUT_ROOT:-/data/xieht/eval_results/nips}"
API_BASE="${API_BASE:-https://open.xiaojingai.com/v1/}"
API_KEY="${API_KEY:?API_KEY env required}"

# ── Local vLLM endpoints (backbones + router checkpoints) ──
Q25_BASE="${Q25_BASE:-http://localhost:8001/v1}"
Q25_MODEL="${Q25_MODEL:-Qwen2.5-7B-Instruct}"
Q3_BASE="${Q3_BASE:-http://localhost:8002/v1}"
Q3_MODEL="${Q3_MODEL:-Qwen3-4B}"
SFT_BASE="${SFT_BASE:-http://localhost:8010/v1}"
SFT_MODEL="${SFT_MODEL:-SkillRouter-SFT}"
RL_BASE="${RL_BASE:-http://localhost:8011/v1}"
RL_MODEL="${RL_MODEL:-SkillRouter-RL}"

STRONGEST_MODEL="${STRONGEST_MODEL:-claude-opus-4-6}"

# ── Parse args ──
SMOKE=false
MAX_TASKS=""
PASS_K=3
VERIFY_WORKERS=4
SELECTED_CELLS=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --smoke) SMOKE=true; shift;;
    --cells) SELECTED_CELLS="$2"; shift 2;;
    --max_tasks) MAX_TASKS="$2"; shift 2;;
    --pass-k) PASS_K="$2"; shift 2;;
    --workers) VERIFY_WORKERS="$2"; shift 2;;
    --dry-run) DRY_RUN=true; shift;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if $SMOKE; then
  MAX_TASKS="${MAX_TASKS:-5}"
  PASS_K=1
  SELECTED_CELLS="${SELECTED_CELLS:-backbone-q25,strongest-q25}"
fi

# ── Cell registry: name | backbone_base | backbone_model | worker_mode | router_base | router_model ──
#   router_base/model only matter when worker_mode=learned
declare -a CELLS=(
  # Direct (backbone is the sub-agent)
  "backbone-q25 | $Q25_BASE | $Q25_MODEL | backbone  | - | -"
  "backbone-q3  | $Q3_BASE  | $Q3_MODEL  | backbone  | - | -"
  # Random (uniform over 9-pool)
  "random-q25   | $Q25_BASE | $Q25_MODEL | random    | - | -"
  "random-q3    | $Q3_BASE  | $Q3_MODEL  | random    | - | -"
  # +claude (always strongest)
  "strongest-q25 | $Q25_BASE | $Q25_MODEL | strongest | - | -"
  "strongest-q3  | $Q3_BASE  | $Q3_MODEL  | strongest | - | -"
  # + (learned router — vanilla LLM prompt)
  "learned-vanilla-q25 | $Q25_BASE | $Q25_MODEL | learned | $Q25_BASE | $Q25_MODEL"
  "learned-vanilla-q3  | $Q3_BASE  | $Q3_MODEL  | learned | $Q3_BASE  | $Q3_MODEL"
  # + (SFT router)
  "learned-sft-q25 | $Q25_BASE | $Q25_MODEL | learned | $SFT_BASE | $SFT_MODEL"
  "learned-sft-q3  | $Q3_BASE  | $Q3_MODEL  | learned | $SFT_BASE | $SFT_MODEL"
  # + (RL router)
  "learned-rl-q25 | $Q25_BASE | $Q25_MODEL | learned | $RL_BASE | $RL_MODEL"
  "learned-rl-q3  | $Q3_BASE  | $Q3_MODEL  | learned | $RL_BASE | $RL_MODEL"
)

should_run() {
  local name="$1"
  [[ -z "$SELECTED_CELLS" ]] && return 0
  [[ ",$SELECTED_CELLS," == *",$name,"* ]]
}

echo "═══════════════════════════════════════════════════════════"
echo " NIPS TB v2 matrix"
echo "   OUT_ROOT:       $OUT_ROOT"
echo "   API_BASE:       $API_BASE"
echo "   max_tasks:      ${MAX_TASKS:-all}"
echo "   pass-k:         $PASS_K"
echo "   verify_workers: $VERIFY_WORKERS"
echo "   cells:          ${SELECTED_CELLS:-ALL}"
echo "═══════════════════════════════════════════════════════════"

mkdir -p "$OUT_ROOT"

cd "$PROJECT_DIR"

for cell in "${CELLS[@]}"; do
  IFS='|' read -r NAME BB_BASE BB_MODEL WMODE RT_BASE RT_MODEL <<< "$cell"
  NAME=$(echo $NAME | xargs); BB_BASE=$(echo $BB_BASE | xargs)
  BB_MODEL=$(echo $BB_MODEL | xargs); WMODE=$(echo $WMODE | xargs)
  RT_BASE=$(echo $RT_BASE | xargs); RT_MODEL=$(echo $RT_MODEL | xargs)

  should_run "$NAME" || continue

  OUT="$OUT_ROOT/${NAME}_terminalbench"
  if [[ -f "$OUT/summary.json" ]]; then
    echo "[SKIP] $NAME (summary exists)"
    continue
  fi
  mkdir -p "$OUT"

  CMD="python -m eval_pipeline \
    --router planner --bench terminalbench \
    --pipeline --pass-k $PASS_K \
    --api_base $API_BASE --api_key $API_KEY \
    --local_base $BB_BASE --local_model $BB_MODEL \
    --worker_mode $WMODE \
    --strongest_model $STRONGEST_MODEL \
    --output_dir $OUT \
    --verify_workers $VERIFY_WORKERS"

  # Router LLM (only for learned mode)
  if [[ "$WMODE" == "learned" ]]; then
    CMD="$CMD --router_model $RT_MODEL"
    # Note: run.py currently uses --local_base for both planner and router;
    # a separate --router_base is a TODO if the two endpoints diverge.
  fi

  if [[ -n "$MAX_TASKS" ]]; then
    CMD="$CMD --max_tasks $MAX_TASKS"
  fi

  echo ""
  echo "────────────────────────────────────────────────────────"
  echo "[RUN] $NAME  (worker=$WMODE, backbone=$BB_MODEL)"
  echo "────────────────────────────────────────────────────────"

  if $DRY_RUN; then
    echo "$CMD"
  else
    eval "$CMD" 2>&1 | tee "$OUT/run.log" || {
      echo "[FAIL] $NAME — see $OUT/run.log"
      continue
    }
  fi
done

echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Done. Collect:"
echo "   python scripts/collect_results.py --root $OUT_ROOT --format md"
echo "═══════════════════════════════════════════════════════════"
