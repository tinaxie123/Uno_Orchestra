#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

exec bash "$PROJECT_DIR/scripts/rl/run_grpo_uno.sh" \
  algorithm.adv_estimator=agentic_grpo \
  algorithm.agentic_grpo.shaping_eta="${AGENTIC_GRPO_SHAPING_ETA:-0.05}" \
  algorithm.agentic_grpo.group_by_parent_prefix=true \
  algorithm.agentic_grpo.group_by_action_type="${AGENTIC_GRPO_GROUP_BY_ACTION_TYPE:-false}" \
  actor_rollout_ref.rollout.multi_turn.agentic_shaping_eta="${AGENTIC_GRPO_SHAPING_ETA:-0.05}" \
  "$@"
