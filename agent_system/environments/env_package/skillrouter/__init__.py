from agent_system.environments.env_package.skillrouter.envs import SkillRouterMultiProcessEnv
from agent_system.environments.env_package.skillrouter.projection import skillrouter_projection


def build_skillrouter_envs(seed=0, env_num=1, group_n=1, is_train=True, env_config=None):
    return SkillRouterMultiProcessEnv(
        seed=seed,
        env_num=env_num,
        group_n=group_n,
        is_train=is_train,
        env_config=env_config,
    )
