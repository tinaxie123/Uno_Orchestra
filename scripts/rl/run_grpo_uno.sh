#!/usr/bin/env bash
# Uno-Orchestra GRPO launcher (4xH100 / single node).
#
# Wires:
#   - UnoAgentLoop      via actor_rollout_ref.rollout.agent.agent_loop_config_path
#   - UnoRewardManager  via reward_model.reward_manager.source=importlib
#                          + module.path / name (no side-effect import needed)
#   - prompt-pool parquets produced by scripts/rl/prepare_prompt_pool.py
#
# Both the agent loop and the reward manager are loaded by hydra/importlib at
# config-resolution time, so the trainer entry never has to know about Uno.
#
# Usage:
#     conda activate multiagentrl
#     cd /data/xieht/multiagentRL
#     bash scripts/rl/run_grpo_uno.sh                    # uses defaults below
#     bash scripts/rl/run_grpo_uno.sh trainer.total_epochs=2   # extra overrides
set -euxo pipefail
export HYDRA_FULL_ERROR=1
export VLLM_USE_V1=1
ulimit -n 65535

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

# Make `scripts.rl.uno_rollout` importable when verl resolves the agent
# loop's `_target_`. The package layout exists (scripts/__init__.py,
# scripts/rl/__init__.py); we just need PROJECT_DIR on sys.path.
export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH:-}"

# Required user inputs — edit or override on the CLI.
MODEL_PATH="${MODEL_PATH:-/data/xieht/checkpoints/Uno-Orchestra-7B-SFT}"
TRAIN_PARQUET="${TRAIN_PARQUET:-$PROJECT_DIR/data/rl/train.parquet}"
VAL_PARQUET="${VAL_PARQUET:-$PROJECT_DIR/data/rl/val.parquet}"

AGENTLOOP_CONFIG_PATH="$PROJECT_DIR/configs/uno/agent.yaml"
REWARD_MODULE_PATH="$PROJECT_DIR/scripts/rl/uno_reward.py"
BASE_CONFIG_PATH="$PROJECT_DIR/verl/examples/sglang_multiturn/config"
HYDRA_TRAINER_CONFIG_PATH="$PROJECT_DIR/verl/verl/trainer/config"

EXP_NAME="${EXP_NAME:-uno-orchestra-grpo}"
PROJECT_NAME="${PROJECT_NAME:-uno-orchestra}"
PYTHON_BIN="${PYTHON_BIN:-python}"

# Per-step rollout dump (chat completions + reward_extra_info as JSONL).
# Default: dumped to a sibling of the ckpt dir. Pass ROLLOUT_DUMP_DIR=off
# (or VAL_DUMP_DIR=off) to disable. JSONL contains: input prompt, decoded
# completion, ground_truth, terminal score, plus every reward_extra_info
# key (done_reason, env_n_route_calls, env_api_cost, env_correctness, ...).
ROLLOUT_DUMP_DIR="${ROLLOUT_DUMP_DIR:-$PROJECT_DIR/checkpoints/uno-orchestra/$EXP_NAME/rollouts}"
VAL_DUMP_DIR="${VAL_DUMP_DIR:-$PROJECT_DIR/checkpoints/uno-orchestra/$EXP_NAME/val_rollouts}"
[[ "$ROLLOUT_DUMP_DIR" == "off" ]] && ROLLOUT_DUMP_DIR=""
[[ "$VAL_DUMP_DIR"     == "off" ]] && VAL_DUMP_DIR=""

# First-surface evidence: a fixed banner so any 0-byte / silent-exit log is
# instantly distinguishable from a real failure deeper in verl. Print BEFORE
# `set -x` echoes the python invocation so this stays at the top of the log.
{ set +x; } 2>/dev/null
echo "============================================================"
echo " Uno-Orchestra GRPO smoke — $(date -Is)"
echo "============================================================"
echo " host           : $(hostname)"
echo " pwd            : $(pwd)"
echo " git HEAD       : $(git rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
echo " python         : $(command -v "$PYTHON_BIN")"
echo " python version : $("$PYTHON_BIN" -V 2>&1)"
echo " CUDA_VISIBLE   : ${CUDA_VISIBLE_DEVICES:-<unset>}"
echo " MODEL_PATH     : $MODEL_PATH"
echo " TRAIN_PARQUET  : $TRAIN_PARQUET"
echo " VAL_PARQUET    : $VAL_PARQUET"
echo " PROJECT_NAME   : $PROJECT_NAME"
echo " EXP_NAME       : $EXP_NAME"
echo " extra args     : $*"
echo "============================================================"
[[ -e "$MODEL_PATH" ]]      || { echo "FATAL: MODEL_PATH not found"   >&2; exit 2; }
[[ -e "$TRAIN_PARQUET" ]]   || { echo "FATAL: TRAIN_PARQUET not found">&2; exit 2; }
[[ -e "$VAL_PARQUET" ]]     || { echo "FATAL: VAL_PARQUET not found"  >&2; exit 2; }
# The Uno env's worker pool calls a remote LLM API for sub-agent skills;
# fail fast here rather than after 30 s of vLLM init if the key isn't set.
[[ -n "${REMOTE_API_KEY:-}" ]] || { echo "FATAL: REMOTE_API_KEY not set in env (worker pool needs it)" >&2; exit 2; }
set -x

"$PYTHON_BIN" -m verl.trainer.main_ppo \
    --config-path="$BASE_CONFIG_PATH" \
    --config-name='gsm8k_multiturn_grpo' \
    "hydra.searchpath=[file://$HYDRA_TRAINER_CONFIG_PATH]" \
    algorithm.adv_estimator=grpo \
    algorithm.use_kl_in_reward=False \
    data.train_files="$TRAIN_PARQUET" \
    data.val_files="$VAL_PARQUET" \
    data.train_batch_size=64 \
    data.max_prompt_length=4096 \
    data.max_response_length=16384 \
    data.filter_overlong_prompts=True \
    data.filter_overlong_prompts_workers=16 \
    data.truncation='error' \
    data.return_raw_chat=True \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=32 \
    actor_rollout_ref.actor.use_dynamic_bsz=True \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=24000 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.fsdp_config.param_offload=True \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    actor_rollout_ref.rollout.name=vllm \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.n=8 \
    actor_rollout_ref.rollout.temperature=1.0 \
    actor_rollout_ref.rollout.enforce_eager=True \
    actor_rollout_ref.rollout.free_cache_engine=True \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=8 \
    actor_rollout_ref.rollout.agent.agent_loop_config_path="$AGENTLOOP_CONFIG_PATH" \
    actor_rollout_ref.rollout.agent.default_agent_loop=uno \
    reward_manager.source=importlib \
    reward_manager.name=UnoRewardManager \
    reward_manager.module.path="$REWARD_MODULE_PATH" \
    reward_model.use_reward_loop=False \
    trainer.critic_warmup=0 \
    trainer.logger='["console","wandb"]' \
    trainer.project_name="$PROJECT_NAME" \
    trainer.experiment_name="$EXP_NAME" \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.save_freq=50 \
    trainer.test_freq=20 \
    trainer.total_epochs=5 \
    trainer.val_before_train=False \
    ${ROLLOUT_DUMP_DIR:+trainer.rollout_data_dir="$ROLLOUT_DUMP_DIR"} \
    ${VAL_DUMP_DIR:+trainer.validation_data_dir="$VAL_DUMP_DIR"} \
    "$@"
