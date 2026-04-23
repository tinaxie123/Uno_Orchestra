#!/usr/bin/env bash
# GRPO training for SkillRouter via Router-R1-style rollout (path B).
#
# Runs the same data + warm-start checkpoint as the GiGPO path
# (run_skillrouter.sh in verl-agent), but swaps the rollout driver from
# verl-agent's env_manager to SkillRouterGenerationManager — the generate→
# splice-obs→generate loop from Router-R1. Used for a head-to-head
# comparison against the GiGPO run.
#
# Usage:
#   CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 bash scripts/rl/run_grpo_skillrouter.sh
set -x

ENGINE=${1:-vllm}

# --- Data (same parquets GiGPO is training on) ---
TRAIN_DATA="${TRAIN_DATA:-/data/xieht/verl-agent/data/skillrouter/train.parquet}"
VAL_DATA="${VAL_DATA:-/data/xieht/verl-agent/data/skillrouter/val.parquet}"

# --- Warm-start SFT ckpt (local) ---
MODEL_PATH="${MODEL_PATH:-/data/xieht/sft/checkpoints/router_qwen25_7b_full_sft/checkpoint-678}"

# --- GRPO ---
group_size=5                 # match GiGPO group size for A/B parity
max_turns=5                  # match max_env_steps=5 from path A
alpha_init=0.1               # R = (1-α) correctness + α cost_reward
train_batch_size=64
ppo_mini_batch_size=32
ppo_micro_batch_size=2
max_prompt_length=4096
max_response_length=2048
max_start_length=2048
max_obs_length=512
total_training_steps=200

# --- Env ---
export VLLM_ATTENTION_BACKEND=FLASH_ATTN
export HYDRA_FULL_ERROR=1
export SKILLROUTER_SYSTEM_PROMPT=/home/xieht/data/sft/system_prompt.txt
# Worker API — xiaojingai proxy serves the real models (claude/gpt/gemini/kimi)
export REMOTE_API_BASE="${REMOTE_API_BASE:-https://open.xiaojingai.com/v1/}"
export REMOTE_API_KEY="${REMOTE_API_KEY:-sk-wFh8h2dhytX3J7ywOZld4IVWoEoBr8hZ8DonD60UYHDZSrYT}"
# So both Router-R1 and verl-agent are importable by the launcher;
# ordering matters — put verl-agent FIRST so `import verl` resolves to
# the verl-agent copy (which has prime_code + our skillrouter env).
export PYTHONPATH="/data/xieht/verl-agent:/data/xieht/Router-R1:${PYTHONPATH}"

PY=/data/conda/envs/verl/bin/python

cd /data/xieht/Router-R1 && $PY /data/xieht/multiagentRL/scripts/rl/launch_grpo.py \
    algorithm.adv_estimator=grpo \
    data.train_files="['$TRAIN_DATA']" \
    data.val_files="['$VAL_DATA']" \
    data.train_data_num=null \
    data.val_data_num=null \
    data.train_batch_size=$train_batch_size \
    data.val_batch_size=100 \
    data.max_prompt_length=$max_prompt_length \
    data.max_response_length=$max_response_length \
    data.max_start_length=$max_start_length \
    data.max_obs_length=$max_obs_length \
    data.return_raw_chat=True \
    data.shuffle_train_dataloader=True \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.optim.lr_warmup_steps_ratio=0.1 \
    actor_rollout_ref.actor.optim.weight_decay=0.01 \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.ppo_mini_batch_size=$ppo_mini_batch_size \
    actor_rollout_ref.actor.ppo_micro_batch_size=$ppo_micro_batch_size \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.grad_clip=1.0 \
    actor_rollout_ref.actor.state_masking=true \
    actor_rollout_ref.actor.fsdp_config.param_offload=False \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=False \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.temperature=1.0 \
    actor_rollout_ref.rollout.top_p=1.0 \
    actor_rollout_ref.rollout.top_k=-1 \
    actor_rollout_ref.rollout.n=$group_size \
    actor_rollout_ref.rollout.n_agent=1 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size=8 \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    actor_rollout_ref.ref.log_prob_micro_batch_size=8 \
    algorithm.kl_ctrl.kl_coef=0.001 \
    algorithm.gamma=0.95 \
    algorithm.no_think_rl=false \
    ++do_route=True \
    ++api_base="$REMOTE_API_BASE" \
    ++api_key="$REMOTE_API_KEY" \
    ++cost_coe=$alpha_init \
    ++max_turns=$max_turns \
    ++reward_metric=f1 \
    trainer.logger='["console","wandb"]' \
    trainer.project_name='skillrouter-rl' \
    trainer.experiment_name="grpo_qwen25_7b_sft_ckpt678_8gpu" \
    trainer.n_gpus_per_node=8 \
    trainer.nnodes=1 \
    trainer.save_freq=50 \
    trainer.test_freq=50 \
    trainer.total_epochs=3 \
    trainer.total_training_steps=$total_training_steps \
    +trainer.val_before_train=True \
    trainer.default_local_dir="/data/xieht/sft/checkpoints/rl_grpo_v1" \
    "$@"
