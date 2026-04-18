"""Docker Compose manager for Terminal Bench."""
import atexit
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel

import logging; logger = logging.getLogger(__name__)


class DockerComposeEnvVars(BaseModel):
    """Environment variables for docker-compose."""

    main_image_name: str
    context_dir: str
    test_dir: str = "/tests"
    host_verifier_logs_path: str
    host_agent_logs_path: str
    env_verifier_logs_path: str = "/logs/verifier"
    env_agent_logs_path: str = "/logs/agent"
    cpus: str = "1"
    memory: str = "2G"

    def to_env_dict(self) -> Dict[str, str]:
        """Convert to environment variable dict."""
        return {
            "MAIN_IMAGE_NAME": self.main_image_name,
            "CONTEXT_DIR": self.context_dir,
            "TEST_DIR": self.test_dir,
            "HOST_VERIFIER_LOGS_PATH": self.host_verifier_logs_path,
            "HOST_AGENT_LOGS_PATH": self.host_agent_logs_path,
            "ENV_VERIFIER_LOGS_PATH": self.env_verifier_logs_path,
            "ENV_AGENT_LOGS_PATH": self.env_agent_logs_path,
            "CPUS": self.cpus,
            "MEMORY": self.memory,
        }


class DockerComposeManager:
    """Thread-safe manager for docker-compose projects."""

    def __init__(self, compose_file_path: Path):
        self.compose_file_path = compose_file_path
        # {project_name: (compose_file_dir, env_vars)}
        self._active_containers: Dict[str, tuple[Path, Optional[Dict[str, str]]]] = {}
        self._lock = threading.Lock()
        self._setup_cleanup_handlers()

    def _setup_cleanup_handlers(self):
        """Setup cleanup handlers for graceful shutdown."""
        atexit.register(self.cleanup_all)
        # signal.signal only works in main thread; skip if called from worker thread
        import threading
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, _frame):
        """Handle termination signals."""
        logger.info(f"Received signal {signum}, cleaning up...")
        self.cleanup_all()
        exit(1)

    def register_project(
        self, project_name: str, compose_dir: Path, env_vars: Optional[Dict[str, str]] = None
    ):
        """Register docker-compose project for cleanup (thread-safe)."""
        with self._lock:
            self._active_containers[project_name] = (compose_dir, env_vars)
            logger.debug(f"Registered project: {project_name}")

    def unregister_project(self, project_name: str):
        """Unregister docker-compose project (thread-safe)."""
        with self._lock:
            self._active_containers.pop(project_name, None)
            logger.debug(f"Unregistered project: {project_name}")

    def cleanup_project(
        self, 
        project_name: str, 
        compose_dir: Path, 
        env_vars: Optional[Dict[str, str]] = None,
        remove_images: bool = True,
    ):
        """
        Clean up a single docker-compose project.
        
        Args:
            project_name: Docker compose project name
            compose_dir: Directory containing the compose file
            env_vars: Environment variables for compose
            remove_images: Whether to remove images (set False for prebuilt images)
        """
        try:
            # Prepare environment variables
            env_dict = os.environ.copy()
            if env_vars:
                env_dict.update(env_vars)

            # Build command arguments
            cmd_args = [
                "docker",
                "compose",
                "-f",
                str(self.compose_file_path),
                "-p",
                project_name,
                "down",
            ]
            
            # Only add --rmi flag if we want to remove images
            if remove_images:
                cmd_args.extend(["--rmi", "all"])
            
            cmd_args.extend(["--volumes", "--remove-orphans"])

            # Use 'docker compose' (new version)
            result = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(compose_dir),
                env=env_dict,
            )

            if result.returncode != 0:
                logger.error(
                    f"Failed to cleanup project {project_name}. "
                    f"Return code: {result.returncode}. "
                    f"Stdout: {result.stdout}. "
                    f"Stderr: {result.stderr}"
                )
            else:
                logger.info(f"Removed docker-compose project: {project_name}")
                self.unregister_project(project_name)
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout while cleaning up project {project_name}")
        except Exception as e:
            logger.error(f"Failed to cleanup project {project_name}: {e}")

    def cleanup_all(self):
        """Clean up all registered docker-compose projects (thread-safe)."""
        with self._lock:
            if not self._active_containers:
                return
            # Create a snapshot to avoid holding lock during cleanup
            projects_snapshot = list(self._active_containers.items())

        logger.info(f"Cleaning up {len(projects_snapshot)} docker-compose project(s)...")
        for project_name, (compose_dir, env_vars) in projects_snapshot:
            self.cleanup_project(project_name, compose_dir, env_vars)
