#!/usr/bin/env bash
# GiGPO training for SkillRouter
#
# Prerequisites:
#   1. verl-agent installed
#   2. SFT warm-start model at $MODEL_PATH
#   3. RL prompt pool prepared via scripts/rl/prepare_prompt_pool.py
#
# Usage:
#   CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 bash scripts/rl/run_gigpo_skillrouter.sh

set -x

ENGINE=${1:-vllm}

# --- Data ---
TRAIN_DATA="${TRAIN_DATA:-/home/xieht/data/sft/rl_train_v2.parquet}"
VAL_DATA="${VAL_DATA:-/home/xieht/data/sft/rl_val_v2.parquet}"

# --- Model (SFT warm-start) ---
MODEL_PATH="${MODEL_PATH:-/home/xieht/data/sft/checkpoints/router_qwen25_7b_full_sft}"

# --- GiGPO config ---
group_size=8           # N=8 episode rollouts per query
mode="mean_std_norm"
enable_similarity=True
similarity_thresh=0.9
step_advantage_w=1.0   # Weight for step-level (route) advantage

# --- Training ---
train_batch_size=128
ppo_mini_batch_size=64
ppo_micro_batch_size_per_gpu=4
max_prompt_length=4096
max_response_length=2048
total_epochs=3
total_training_steps=500
save_freq=50
test_freq=50

# --- Env ---
max_env_steps=3        # Max rounds (plan→obs→verify cycles)
alpha=0.1              # Router-R1: R = (1-α)*R_outcome + α*R_cost

export VLLM_ATTENTION_BACKEND=FLASH_ATTN
export HYDRA_FULL_ERROR=1

python3 -m verl.trainer.main_ppo \
    algorithm.adv_estimator=gigpo \
    data.train_files="['$TRAIN_DATA']" \
    data.val_files="['$VAL_DATA']" \
    data.train_batch_size=$train_batch_size \
    data.val_batch_size=256 \
    data.max_prompt_length=$max_prompt_length \
    data.max_response_length=$max_response_length \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    data.return_raw_chat=True \
    data.shuffle=True \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.optim.lr_warmup_steps_ratio=0.1 \
    actor_rollout_ref.actor.optim.weight_decay=0.01 \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.ppo_mini_batch_size=$ppo_mini_batch_size \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=$ppo_micro_batch_size_per_gpu \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.grad_clip=1.0 \
    actor_rollout_ref.actor.fsdp_config.param_offload=False \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=False \
    actor_rollout_ref.actor.use_invalid_action_penalty=True \
    actor_rollout_ref.actor.invalid_action_penalty_coef=0.01 \
    actor_rollout_ref.rollout.n=$group_size \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.enable_chunked_prefill=True \
    actor_rollout_ref.rollout.temperature=1.0 \
    actor_rollout_ref.rollout.top_p=1.0 \
    actor_rollout_ref.rollout.top_k=-1 \
    actor_rollout_ref.rollout.val_kwargs.temperature=0.3 \
    actor_rollout_ref.rollout.val_kwargs.top_p=0.9 \
    actor_rollout_ref.rollout.val_kwargs.do_sample=True \
    actor_rollout_ref.rollout.val_kwargs.n=1 \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=16 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=16 \
    algorithm.use_kl_in_reward=False \
    algorithm.gamma=0.95 \
    algorithm.gigpo.step_advantage_w=$step_advantage_w \
    algorithm.gigpo.mode=$mode \
    algorithm.gigpo.enable_similarity=$enable_similarity \
    algorithm.gigpo.similarity_thresh=$similarity_thresh \
    env.env_name=skillrouter \
    env.seed=42 \
    env.max_steps=$max_env_steps \
    env.rollout.n=$group_size \
    +env.skillrouter.alpha=$alpha \
    reward_model.reward_manager=episode \
    trainer.logger='["console","wandb"]' \
    trainer.project_name='skillrouter-rl' \
    trainer.experiment_name="gigpo_qwen25_7b_sft_warmstart" \
    trainer.n_gpus_per_node=8 \
    trainer.nnodes=1 \
    trainer.save_freq=$save_freq \
    trainer.test_freq=$test_freq \
    trainer.total_epochs=$total_epochs \
    trainer.total_training_steps=$total_training_steps \
    trainer.val_before_train=True \
    trainer.default_local_dir="/home/xieht/data/sft/checkpoints/rl_gigpo" \
    trainer.resume_mode=auto \
    "$@"
