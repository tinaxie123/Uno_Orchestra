#!/usr/bin/env bash
# GRPO training for SkillRouter v4
#
# Reward: R = (1-α) * R_correctness + α * R_cost (adaptive α)
#
# Usage:
#   CUDA_VISIBLE_DEVICES=4,5,6,7 bash scripts/rl/run_grpo_skillrouter_v4.sh

set -x

ENGINE=${1:-vllm}

# --- Data ---
TRAIN_DATA="${TRAIN_DATA:-/data/xieht/multiagentRL/data/sft/merged/rl_train_v4.parquet}"
VAL_DATA="${VAL_DATA:-/data/xieht/multiagentRL/data/sft/merged/rl_val_v4.parquet}"

# --- Model (SFT v4 warm-start) ---
MODEL_PATH="${MODEL_PATH:-/home/xieht/data/LlamaFactory/outputs/router_qwen25_7b_sft_v4}"

# --- GRPO ---
group_size=8

# --- Reward ---
alpha_init=0.05

export VLLM_ATTENTION_BACKEND=FLASH_ATTN
export HYDRA_FULL_ERROR=1

cd /data/xieht/Router-R1 && python3 /data/xieht/multiagentRL/scripts/rl/launch_grpo.py \
    algorithm.adv_estimator=grpo \
    data.train_files="['$TRAIN_DATA']" \
    data.val_files="['$VAL_DATA']" \
    data.train_batch_size=64 \
    data.val_batch_size=128 \
    data.max_prompt_length=4096 \
    data.max_response_length=2048 \
    data.return_raw_chat=True \
    data.shuffle_train_dataloader=True \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=5e-7 \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.ppo_mini_batch_size=32 \
    actor_rollout_ref.actor.ppo_micro_batch_size=2 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0.01 \
    actor_rollout_ref.actor.grad_clip=1.0 \
    actor_rollout_ref.actor.fsdp_config.param_offload=False \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=False \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.temperature=1.0 \
    actor_rollout_ref.rollout.top_p=1.0 \
    actor_rollout_ref.rollout.top_k=-1 \
    actor_rollout_ref.rollout.n=$group_size \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    actor_rollout_ref.ref.log_prob_micro_batch_size=8 \
    algorithm.gamma=0.95 \
    ++do_route=True \
    ++api_base="https://dashscope.aliyuncs.com/compatible-mode/v1" \
    ++api_key="${DASHSCOPE_API_KEY:-sk-70793c3ec75a40ca90b7076de9927260}" \
    ++cost_coe=$alpha_init \
    ++max_turns=3 \
    ++reward_metric=f1 \
    trainer.logger='["console","wandb"]' \
    trainer.project_name='skillrouter-rl' \
    trainer.experiment_name="grpo_qwen25_7b_sft_v4" \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.save_freq=30 \
    trainer.test_freq=30 \
    trainer.total_epochs=3 \
    trainer.total_training_steps=300 \
    trainer.default_local_dir="/data/xieht/multiagentRL/outputs/rl_grpo_v4" \
    "$@"
