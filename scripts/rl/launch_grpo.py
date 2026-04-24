import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "/data/xieht/verl-agent" not in sys.path:
    sys.path.insert(0, "/data/xieht/verl-agent")
if "/data/xieht/Router-R1" not in sys.path:
    sys.path.insert(0, "/data/xieht/Router-R1")

import verl.trainer.main_ppo as main_ppo_module
from reward_manager import (
    SkillRouterRewardManager,
    normalize_cost,
    route_count,
)
main_ppo_module.RewardManager = SkillRouterRewardManager
main_ppo_module.normalize_reward = normalize_cost
main_ppo_module.route_count = route_count
main_ppo_module.format_reward = lambda completion: 0.0
from skillrouter_generation import GenerationConfig, SkillRouterGenerationManager
import router_r1.llm_agent.generation as gen_module
gen_module.LLMGenerationManager = SkillRouterGenerationManager
gen_module.GenerationConfig = GenerationConfig

import verl.trainer.ppo.ray_trainer as ray_trainer_module
ray_trainer_module.LLMGenerationManager = SkillRouterGenerationManager
ray_trainer_module.GenerationConfig = GenerationConfig
import verl.protocol as _protocol
_orig_pop = _protocol.DataProto.pop


def _pop_preserving_env_kwargs(
    self, batch_keys=None, non_tensor_batch_keys=None, meta_info_keys=None,
):
    popped = _orig_pop(
        self,
        batch_keys=batch_keys,
        non_tensor_batch_keys=non_tensor_batch_keys,
        meta_info_keys=meta_info_keys,
    )
    for key in ("env_kwargs", "reward_model", "data_source", "extra_info"):
        if key in self.non_tensor_batch:
            popped.non_tensor_batch[key] = self.non_tensor_batch[key]
    return popped


_protocol.DataProto.pop = _pop_preserving_env_kwargs


if __name__ == "__main__":
    main_ppo_module.main()
