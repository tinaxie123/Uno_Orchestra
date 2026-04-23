#!/usr/bin/env bash
# GiGPO training for SkillRouter (4 GPUs, local SFT warm-start).
set -x

ENGINE=${1:-vllm}

# --- Data ---
TRAIN_DATA="/data/xieht/verl-agent/data/skillrouter/train.parquet"
VAL_DATA="/data/xieht/verl-agent/data/skillrouter/val.parquet"

# --- Model (local SFT warm-start) ---
MODEL_PATH="${MODEL_PATH:-/data/xieht/sft/checkpoints/router_qwen25_7b_full_sft/checkpoint-678}"

# --- GiGPO config ---
group_size=5
mode="mean_std_norm"
enable_similarity=True
similarity_thresh=0.9
step_advantage_w=1.0

# --- Training ---
train_batch_size=64
ppo_mini_batch_size=32
ppo_micro_batch_size_per_gpu=2
max_prompt_length=4096
max_response_length=2048
total_training_steps=200
save_freq=50
test_freq=50

# --- Env ---
max_env_steps=5
alpha=0.1

export VLLM_ATTENTION_BACKEND=FLASH_ATTN
export HYDRA_FULL_ERROR=1
export SKILLROUTER_SYSTEM_PROMPT=/home/xieht/data/sft/system_prompt.txt
# Worker API endpoint — xiaojingai proxies real models (claude/gpt/gemini/kimi)
# directly, no Qwen tier remapping; override if rotating keys.
export REMOTE_API_BASE="${REMOTE_API_BASE:-https://open.xiaojingai.com/v1/}"
export REMOTE_API_KEY="${REMOTE_API_KEY:-sk-wFh8h2dhytX3J7ywOZld4IVWoEoBr8hZ8DonD60UYHDZSrYT}"
export PYTHONPATH=/data/xieht/verl-agent

PY=/data/conda/envs/verl/bin/python

$PY -m verl.trainer.main_ppo \
    algorithm.adv_estimator=gigpo \
    data.train_files="['$TRAIN_DATA']" \
    data.val_files="['$VAL_DATA']" \
    data.train_batch_size=$train_batch_size \
    data.val_batch_size=100 \
    data.max_prompt_length=$max_prompt_length \
    data.max_response_length=$max_response_length \
    data.filter_overlong_prompts=True \
    data.filter_overlong_prompts_workers=8 \
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
    actor_rollout_ref.rollout.n=1 \
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
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=8 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=8 \
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
    +env.alpha=$alpha \
    reward_model.reward_manager=episode \
    trainer.logger='["console","wandb"]' \
    trainer.project_name='skillrouter-rl' \
    trainer.experiment_name="gigpo_qwen25_7b_sft_warmstart_8gpu" \
    trainer.n_gpus_per_node=8 \
    trainer.nnodes=1 \
    trainer.save_freq=$save_freq \
    trainer.test_freq=$test_freq \
    trainer.total_training_steps=$total_training_steps \
    trainer.val_before_train=True \
    trainer.default_local_dir="/data/xieht/sft/checkpoints/rl_gigpo_v1" \
    trainer.resume_mode=auto \
    "$@"
