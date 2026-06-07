"""Docker executor for Terminal Bench tasks."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple

import logging; logger = logging.getLogger(__name__)
from .base_executor import BaseExecutor
from .docker_manager import DockerComposeEnvVars, DockerComposeManager


def _container_proxy_exports() -> str:
    """Return shell exports for optional container proxy settings."""
    mappings = {
        "http_proxy": os.environ.get("TBENCH_HTTP_PROXY") or os.environ.get("http_proxy"),
        "https_proxy": os.environ.get("TBENCH_HTTPS_PROXY") or os.environ.get("https_proxy"),
        "all_proxy": os.environ.get("TBENCH_ALL_PROXY") or os.environ.get("all_proxy"),
        "no_proxy": os.environ.get("TBENCH_NO_PROXY") or os.environ.get("no_proxy"),
    }
    exports = []
    for key, value in mappings.items():
        if not value:
            continue
        exports.append(f"export {key}={sh_quote(value)} {key.upper()}={sh_quote(value)}")
    return "; ".join(exports)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


class DockerExecutor(BaseExecutor):
    """Executes Terminal Bench tasks in Docker containers."""

    def __init__(
        self,
        task_id: str,
        task_dir: Path,
        task_config: dict,
        verifier_logs_dir: Path,
        agent_logs_dir: Path,
        docker_manager: DockerComposeManager,
        docker_timeout: int = 600,
        env_init: Optional[dict[str, str]] = None,
    ):
        super().__init__(
            task_id=task_id,
            task_dir=task_dir,
            task_config=task_config,
            verifier_logs_dir=verifier_logs_dir,
            agent_logs_dir=agent_logs_dir,
            timeout=docker_timeout,
            env_init=env_init,
        )
        self.docker_manager = docker_manager
        self.docker_timeout = docker_timeout

        self.container_id: Optional[str] = None
        self.image_name: Optional[str] = None
        self.project_name: Optional[str] = None
        self.env_vars: Optional[DockerComposeEnvVars] = None
        self._temp_dockerfile_dir: Optional[Path] = None
        self._use_prebuilt_image: bool = False  # Track if using prebuilt image

    def _create_temp_dockerfile_with_proxy(self, original_dockerfile_dir: Path) -> Path:
        """
        Create a temporary copy of Dockerfile with proxy environment variables injected.
        Returns the temporary directory path.
        """
        # Create temporary directory
        temp_dir = Path(tempfile.mkdtemp(prefix=f"tbench_{self.task_id}_"))
        self._temp_dockerfile_dir = temp_dir

        # Copy entire environment directory to temp
        original_env_dir = original_dockerfile_dir
        temp_env_dir = temp_dir / "environment"
        shutil.copytree(original_env_dir, temp_env_dir, symlinks=True)

        # Inject proxy variables into the copied Dockerfile
        temp_dockerfile = temp_env_dir / "Dockerfile"
        if self.env_init:
            original_content = temp_dockerfile.read_text()
            lines = original_content.split("\n")

            # Build ENV instructions
            env_instructions = []
            for key, value in self.env_init.items():
                if value:
                    env_instructions.append(f"ENV {key}={value}")

            if env_instructions:
                # Insert ENV after every FROM to support multi-stage builds
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    # Insert ENV after each FROM instruction (not FROM ... AS ...)
                    stripped = line.strip().upper()
                    if stripped.startswith("FROM "):
                        new_lines.extend(env_instructions)

                temp_dockerfile.write_text("\n".join(new_lines))

        return temp_env_dir

    async def start_container(self):
        """Start Docker container using docker compose (runs in thread pool)."""
        # Get environment config from task.toml
        env_config = self.task_config.get("environment", {})
        prebuilt_image = env_config.get("docker_image")  # e.g., "alexgshaw/extract-elf:20251031"
        cpus = env_config.get("cpus", 1)
        memory = env_config.get("memory", "2G")

        # Generate unique project name
        session_id = str(uuid.uuid4())[:8]
        self.project_name = f"tbench-{self.task_id}-{session_id}".lower().replace("_", "-")

        # Determine whether to use prebuilt image or build from Dockerfile
        use_prebuilt = prebuilt_image is not None
        self._use_prebuilt_image = use_prebuilt
        
        if use_prebuilt:
            # Use prebuilt image directly
            logger.info(f"Using prebuilt image: {prebuilt_image}")
            self.image_name = prebuilt_image
            build_context_dir = self.task_dir / "environment"  # Still need context dir for compose
        else:
            # Build from Dockerfile
            dockerfile_dir = self.task_dir / "environment"
            dockerfile_path = dockerfile_dir / "Dockerfile"
            if not dockerfile_path.exists():
                raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")
            
            logger.info(f"Building image from Dockerfile: {dockerfile_path}")
            self.image_name = f"tbench-{self.task_id}-{session_id}".lower().replace("_", "-")
            
            # Create temporary Dockerfile with proxy if needed
            build_context_dir = dockerfile_dir
            if self.env_init:
                build_context_dir = self._create_temp_dockerfile_with_proxy(dockerfile_dir)

        try:
            # Setup environment variables for docker-compose
            self.env_vars = DockerComposeEnvVars(
                main_image_name=self.image_name,
                context_dir=str(build_context_dir.resolve().absolute()),
                test_dir="/tests",
                host_verifier_logs_path=str(self.verifier_logs_dir.resolve().absolute()),
                host_agent_logs_path=str(self.agent_logs_dir.resolve().absolute()),
                env_verifier_logs_path="/logs/verifier",
                env_agent_logs_path="/logs/agent",
                cpus=str(cpus),
                memory=str(memory),
            )

            env_dict = os.environ.copy()
            env_dict.update(self.env_vars.to_env_dict())

            if use_prebuilt:
                # Pull prebuilt image if not exists locally
                logger.info(f"Checking image: {self.image_name}")
                
                # Check if image already exists locally
                check_result = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "images", "-q", self.image_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                
                if check_result.stdout.strip():
                    logger.info(f"✓ Image already exists locally")
                else:
                    # Pull image quietly
                    logger.info(f"Pulling image from registry (this may take a while)...")
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "pull", self.image_name],
                        capture_output=True,
                        text=True,
                        timeout=self.docker_timeout,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(f"Failed to pull image: {result.stderr}")
                    logger.info(f"✓ Image pulled successfully")
            else:
                # Build image from Dockerfile (in thread pool to avoid blocking)
                logger.info(f"Building image: {self.image_name}")
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(self.docker_manager.compose_file_path),
                        "-p",
                        self.project_name,
                        "build",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.docker_timeout,
                    env=env_dict,
                    cwd=str(build_context_dir),
                )

                if result.returncode != 0:
                    raise RuntimeError(f"Failed to build image: {result.stderr}")
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self.docker_manager.compose_file_path),
                    "-p",
                    self.project_name,
                    "up",
                    "-d",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env_dict,
                cwd=str(build_context_dir),
            )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to start container: {result.stderr}")

            # Get container ID (in thread pool)
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self.docker_manager.compose_file_path),
                    "-p",
                    self.project_name,
                    "ps",
                    "-q",
                    "main",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=env_dict,
                cwd=str(build_context_dir),
            )

            self.container_id = result.stdout.strip()
            if not self.container_id:
                raise RuntimeError("Container ID is empty")

            # Register project for cleanup
            env_dict_for_cleanup = self.env_vars.to_env_dict() if self.env_vars else None
            self.docker_manager.register_project(
                self.project_name, build_context_dir, env_dict_for_cleanup
            )

        except subprocess.TimeoutExpired as e:
            # Ensure docker resources are released on build/run timeout
            await self.cleanup()
            raise RuntimeError(f"Docker operation timed out: {e}") from e
        except Exception as e:
            # Ensure docker resources are released on any start failure
            await self.cleanup()
            raise RuntimeError(f"Failed to start container: {e}") from e

    async def execute_command(self, command: str, timeout: Optional[int] = None) -> Tuple[str, int]:
        """Execute command in container."""
        if not self.container_id:
            raise RuntimeError("Container not started")

        # Use provided timeout or default
        exec_timeout = timeout if timeout is not None else self.docker_timeout

        proxy_exports = _container_proxy_exports()
        wrapped = f"{proxy_exports}; {command}" if proxy_exports else command

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", self.container_id, "sh", "-c", wrapped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=exec_timeout
            )

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0

            return output, exit_code

        except asyncio.TimeoutError:
            return "Command timed out", -1
        except Exception as e:
            return f"Error executing command: {e}", -1
    
    async def run_tests(self) -> float:
        """Run tests and return reward."""
        if not self.container_id:
            raise RuntimeError("Container not started")

        test_script = self.task_dir / "tests" / "test.sh"
        if not test_script.exists():
            logger.error(f"Test script not found: {test_script}")
            return 0.0

        try:
            # Create /tests directory in container (async)
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", self.container_id, "mkdir", "-p", "/tests"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Copy test script to container (async)
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "cp", str(test_script), f"{self.container_id}:/tmp/test.sh"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Copy test files to container (async)
            tests_dir = self.task_dir / "tests"
            if tests_dir.exists():
                for test_file in tests_dir.glob("*.py"):
                    await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "cp", str(test_file), f"{self.container_id}:/tests/"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

            proxy_exports = _container_proxy_exports()
            proxy_setup = f"{proxy_exports}; bash /tmp/test.sh" if proxy_exports else "bash /tmp/test.sh"
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", self.container_id, "bash", "-c", proxy_setup,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            # Get test timeout from task config
            test_timeout = self.task_config.get("verifier", {}).get("timeout_sec", 900)

            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=test_timeout
            )

            output = stdout.decode("utf-8", errors="replace")

            # Save test output to verifier log file
            test_log = self.verifier_logs_dir / "test_output.log"
            with test_log.open("w", encoding="utf-8") as f:
                f.write("Test Execution Output\n")
                f.write("=" * 80 + "\n")
                f.write(output)
                f.write("\n" + "=" * 80 + "\n")

            # Read reward from /logs/verifier/reward.txt (async)
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", self.container_id, "cat", "/logs/verifier/reward.txt"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                reward_str = result.stdout.strip()
                try:
                    reward = float(reward_str)
                    return reward
                except ValueError:
                    logger.error(f"Invalid reward value: {reward_str}")
                    return 0.0
            else:
                logger.error("Failed to read reward file")
                return 0.0

        except asyncio.TimeoutError:
            logger.error("Test execution timed out")
            return 0.0
        except Exception as e:
            logger.error(f"Error running tests: {e}")
            return 0.0

    def _cleanup_temp_dockerfile(self):
        """Clean up temporary Dockerfile directory."""
        if self._temp_dockerfile_dir and self._temp_dockerfile_dir.exists():
            try:
                shutil.rmtree(self._temp_dockerfile_dir)
                logger.debug(f"Cleaned up temp Dockerfile directory: {self._temp_dockerfile_dir}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp Dockerfile directory: {e}")
            finally:
                self._temp_dockerfile_dir = None

    def get_container_id(self) -> Optional[str]:
        """Get the container ID."""
        return self.container_id

    async def cleanup(self):
        """Clean up Docker container and image using docker compose."""
        try:
            if self.project_name:
                # Determine build context directory
                # Use temp dir if it exists, otherwise use original dir
                if self._temp_dockerfile_dir and self._temp_dockerfile_dir.exists():
                    build_context_dir = self._temp_dockerfile_dir
                else:
                    build_context_dir = self.task_dir / "environment"

                env_dict = self.env_vars.to_env_dict() if self.env_vars else None
                
                # Don't remove prebuilt images (they may be used by other tasks)
                remove_images = not self._use_prebuilt_image
                if self._use_prebuilt_image:
                    logger.info(f"Keeping prebuilt image: {self.image_name}")
                
                # Synchronous cleanup call
                self.docker_manager.cleanup_project(
                    self.project_name, 
                    build_context_dir, 
                    env_dict,
                    remove_images=remove_images
                )
        finally:
            # Always clean up temporary Dockerfile directory, even if docker cleanup fails
            self._cleanup_temp_dockerfile()
