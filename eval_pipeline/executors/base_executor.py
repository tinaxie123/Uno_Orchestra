"""Base executor interface for Terminal Bench tasks."""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Tuple


class BaseExecutor(ABC):
    """Abstract base class for task executors (Docker, E2B, etc.)."""

    def __init__(
        self,
        task_id: str,
        task_dir: Path,
        task_config: dict,
        verifier_logs_dir: Path,
        agent_logs_dir: Path,
        timeout: int = 600,
        env_init: Optional[dict[str, str]] = None,
    ):
        self.task_id = task_id
        self.task_dir = task_dir
        self.task_config = task_config
        self.verifier_logs_dir = verifier_logs_dir
        self.agent_logs_dir = agent_logs_dir
        self.timeout = timeout
        self.env_init = env_init or {}

    @abstractmethod
    async def start_container(self):
        """Start the execution environment (container/sandbox)."""
        pass

    @abstractmethod
    async def execute_command(self, command: str, timeout: Optional[int] = None) -> Tuple[str, int]:
        """
        Execute a command in the environment.
        
        Returns:
            Tuple of (output, exit_code)
        """
        pass

    @abstractmethod
    async def run_tests(self) -> float:
        """
        Run the test script and return reward.
        
        Returns:
            Reward value (0.0 or 1.0)
        """
        pass

    @abstractmethod
    async def cleanup(self):
        """Clean up the execution environment."""
        pass

    @abstractmethod
    def get_container_id(self) -> Optional[str]:
        """Get the container/sandbox ID."""
        pass

