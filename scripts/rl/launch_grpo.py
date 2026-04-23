"""
Launch GRPO training with SkillRouter multi-turn generation + reward.

Patches Router-R1's verl to use:
  1. SkillRouterGenerationManager — multi-turn loop with real API calls
  2. SkillRouterRewardManager — per-source verifiers + cost reward

Usage:
    cd /data/xieht/Router-R1 && python /path/to/launch_grpo.py [hydra overrides...]
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Patch 1: RewardManager ──
import verl.trainer.main_ppo as main_ppo_module
from reward_manager import SkillRouterRewardManager, normalize_cost, format_reward, route_count
main_ppo_module.RewardManager = SkillRouterRewardManager
main_ppo_module.normalize_reward = normalize_cost
main_ppo_module.format_reward = format_reward
main_ppo_module.route_count = route_count

# ── Patch 2: LLMGenerationManager ──
from skillrouter_generation import SkillRouterGenerationManager, GenerationConfig
import router_r1.llm_agent.generation as gen_module
gen_module.LLMGenerationManager = SkillRouterGenerationManager
gen_module.GenerationConfig = GenerationConfig

# Also patch the import in ray_trainer
import verl.trainer.ppo.ray_trainer as ray_trainer_module
ray_trainer_module.LLMGenerationManager = SkillRouterGenerationManager
ray_trainer_module.GenerationConfig = GenerationConfig

if __name__ == '__main__':
    main_ppo_module.main()
