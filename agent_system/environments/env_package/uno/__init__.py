from agent_system.environments.env_package.uno.envs import UnoMultiProcessEnv
from agent_system.environments.env_package.uno.projection import uno_projection


def build_uno_envs(seed=0, env_num=1, group_n=1, is_train=True, env_config=None):
    return UnoMultiProcessEnv(
        seed=seed,
        env_num=env_num,
        group_n=group_n,
        is_train=is_train,
        env_config=env_config,
    )
