"""SWE-bench executor - based on official swebench harness implementation."""
import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import logging; logger = logging.getLogger(__name__)
from .swebench_data_loader import SWEBenchInstance

# ============================================================================
# Constants from official swebench (swebench/harness/constants.py)
# ============================================================================

START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"

NON_TEST_EXTS = [
    ".json", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico",
    ".txt", ".md", ".rst", ".csv", ".tsv", ".xml", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".conf", ".lock", ".log",
]

# Repository-specific test commands (simplified from MAP_REPO_VERSION_TO_SPECS)
REPO_TEST_CMDS = {
    "astropy/astropy": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "django/django": "./tests/runtests.py --verbosity 2 {tests}",
    "matplotlib/matplotlib": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pallets/flask": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "psf/requests": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pylint-dev/pylint": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pytest-dev/pytest": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "scikit-learn/scikit-learn": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "sphinx-doc/sphinx": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "sympy/sympy": "bin/test -C --verbose {tests}",
    "pydata/xarray": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "mwaskom/seaborn": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
}

DEFAULT_TEST_CMD = "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider"


# ============================================================================
# Utility functions from official swebench
# ============================================================================

def get_modified_files(patch: str) -> List[str]:
    """Extract list of modified files from a patch (from swebench/harness/utils.py)."""
    diff_pat = r"diff --git a/.* b/(.*)"
    return re.findall(diff_pat, patch)


def get_test_directives(repo: str, test_patch: str) -> List[str]:
    """
    Get test directives from the test_patch of a task instance.
    Based on swebench/harness/test_spec/python.py:get_test_directives
    """
    diff_pat = r"diff --git a/.* b/(.*)"
    directives = re.findall(diff_pat, test_patch)
    directives = [
        d for d in directives if not any(d.endswith(ext) for ext in NON_TEST_EXTS)
    ]
    
    # For Django tests, remove extension + "tests/" prefix and convert slashes to dots
    if repo == "django/django":
        directives_transformed = []
        for d in directives:
            d = d[: -len(".py")] if d.endswith(".py") else d
            d = d[len("tests/") :] if d.startswith("tests/") else d
            d = d.replace("/", ".")
            directives_transformed.append(d)
        directives = directives_transformed
    
    return directives


def make_eval_script(
    repo: str,
    base_commit: str,
    test_patch: str,
    repo_directory: str = "/testbed",
    env_name: str = "testbed",
) -> str:
    """
    Generate evaluation script based on official swebench implementation.
    Based on swebench/harness/test_spec/python.py:make_eval_script_list_py
    """
    HEREDOC_DELIMITER = "EOF_114329324912"
    
    # Get test files and directives
    test_files = get_modified_files(test_patch) if test_patch else []
    test_files_str = " ".join(test_files) if test_files else ""
    
    test_directives = get_test_directives(repo, test_patch) if test_patch else []
    directives_str = " ".join(test_directives) if test_directives else ""
    
    # Get test command for repo
    test_cmd_template = REPO_TEST_CMDS.get(repo, DEFAULT_TEST_CMD)
    test_cmd = test_cmd_template.format(tests=directives_str)
    
    # Reset test files command
    reset_tests_command = f"git checkout {base_commit} -- {test_files_str}" if test_files_str else ":"
    
    # Apply test patch command
    apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{test_patch}\n{HEREDOC_DELIMITER}"
        if test_patch else ":"
    )
    
    # Build eval script following official pattern
    eval_commands = [
        "#!/bin/bash",
        "set -uxo pipefail",  # Don't use -e to allow tests to fail
        "",
        "# Activate conda environment",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
        "",
        f"git config --global --add safe.directory {repo_directory}",
        f"cd {repo_directory}",
        "",
        "# Informational output",
        "git status",
        "git show --stat",
        f"git -c core.fileMode=false diff {base_commit}",
        "",
        "# Re-activate environment",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        "",
        "# Reset test files to base commit state",
        reset_tests_command,
        "",
        "# Apply test patch",
        apply_test_patch_command,
        "",
        "# Run tests",
        f": '{START_TEST_OUTPUT}'",
        test_cmd,
        f": '{END_TEST_OUTPUT}'",
        "",
        "# Revert test files",
        reset_tests_command,
    ]
    
    return "\n".join(eval_commands) + "\n"


def parse_log_pytest(log: str) -> Dict[str, str]:
    """
    Parse pytest output log to get test status.
    Based on swebench/harness/log_parsers/pytest_log_parser.py
    """
    test_status = {}
    
    # Pattern for pytest output: "test_file.py::test_name PASSED/FAILED/SKIPPED/ERROR"
    pattern = r"^(.*?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
    
    for line in log.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            test_name = match.group(1).strip()
            status = match.group(2)
            test_status[test_name] = status
    
    return test_status


def parse_log_django(log: str) -> Dict[str, str]:
    """
    Parse Django test output log.
    Based on swebench/harness/log_parsers/django_log_parser.py
    """
    test_status = {}
    
    # Pattern for Django output: "test_name (module.ClassName) ... ok/FAIL/ERROR"
    pattern = r"^(test_\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)"
    
    for line in log.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            test_name = f"{match.group(1)} ({match.group(2)})"
            status_map = {"ok": "PASSED", "FAIL": "FAILED", "ERROR": "FAILED", "skipped": "SKIPPED"}
            test_status[test_name] = status_map.get(match.group(3), "FAILED")
    
    return test_status


def get_eval_tests_report(
    test_output: str,
    repo: str,
    fail_to_pass: List[str],
    pass_to_pass: List[str],
) -> Dict[str, Any]:
    """
    Parse test output and generate evaluation report.
    Based on swebench/harness/grading.py
    """
    # Extract test output between markers
    start_idx = test_output.find(START_TEST_OUTPUT)
    end_idx = test_output.find(END_TEST_OUTPUT)
    
    if start_idx != -1 and end_idx != -1:
        test_log = test_output[start_idx:end_idx]
    else:
        test_log = test_output
    
    # Parse based on repo type
    if repo == "django/django":
        test_status = parse_log_django(test_log)
    else:
        test_status = parse_log_pytest(test_log)
    
    # Classify results
    results = {
        "FAIL_TO_PASS": {"success": [], "failure": []},
        "PASS_TO_PASS": {"success": [], "failure": []},
    }
    
    def test_passed(test_name: str) -> bool:
        """Check if a test passed by matching against parsed status."""
        # Direct match
        if test_name in test_status:
            return test_status[test_name] == "PASSED"
        
        # Partial match - check if test name is contained in any key
        for key, status in test_status.items():
            if test_name in key or key in test_name:
                return status == "PASSED"
            # Also check just the method name
            test_method = test_name.split("::")[-1] if "::" in test_name else test_name.split(".")[-1]
            if test_method in key:
                return status == "PASSED"
        
        # Check for overall pass (e.g., "OK" at end for Django, or "X passed" for pytest)
        if "OK" in test_log.split("\n")[-10:]:
            return True
        if re.search(r"\d+ passed", test_log):
            return True
        
        return False
    
    def test_failed(test_name: str) -> bool:
        """Check if a test failed."""
        if test_name in test_status:
            return test_status[test_name] == "FAILED"
        
        for key, status in test_status.items():
            if test_name in key or key in test_name:
                return status == "FAILED"
            test_method = test_name.split("::")[-1] if "::" in test_name else test_name.split(".")[-1]
            if test_method in key:
                return status == "FAILED"
        
        return False
    
    # Classify FAIL_TO_PASS tests
    for test in fail_to_pass or []:
        if test_passed(test):
            results["FAIL_TO_PASS"]["success"].append(test)
        else:
            results["FAIL_TO_PASS"]["failure"].append(test)
    
    # Classify PASS_TO_PASS tests
    for test in pass_to_pass or []:
        if test_failed(test):
            results["PASS_TO_PASS"]["failure"].append(test)
        else:
            results["PASS_TO_PASS"]["success"].append(test)
    
    return results



class SWEBenchExecutor:
    """Executes SWE-bench tasks using Docker containers."""

    def __init__(
        self,
        instance: SWEBenchInstance,
        logs_dir: Path,
        timeout: int = 1800,
        env_init: Optional[Dict[str, str]] = None,
    ):
        self.instance = instance
        self.logs_dir = logs_dir
        self.timeout = timeout
        self.env_init = env_init or {}
        
        self.container_id: Optional[str] = None
        self._temp_dir: Optional[Path] = None
        self._repo_path: Optional[str] = None  # Linux path in container, use str not Path
        
        # Create logs directory
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def start_container(self):
        """Start Docker container for the SWE-bench instance."""
        # Create temporary directory for workspace
        self._temp_dir = Path(tempfile.mkdtemp(prefix=f"swebench_{self.instance.instance_id}_"))
        
        # Determine image name based on instance_id
        # SWE-bench official images use format: swebench/sweb.eval.x86_64.{owner}_1776_{owner}-{issue}
        # Example: astropy__astropy-12907 -> swebench/sweb.eval.x86_64.astropy_1776_astropy-12907
        # Parse instance_id: "astropy__astropy-12907" -> owner="astropy", issue="12907"
        parts = self.instance.instance_id.split("__")
        owner = parts[0]  # "astropy"
        repo_issue = parts[1] if len(parts) > 1 else self.instance.instance_id  # "astropy-12907"
        image_name = f"swebench/sweb.eval.x86_64.{owner}_1776_{repo_issue}"
        
        logger.info(f"Starting container for {self.instance.instance_id}")
        logger.info(f"Image: {image_name}")
        
        try:
            # Check if image exists locally
            check_result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "images", "-q", image_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            if not check_result.stdout.strip():
                # Pull image
                logger.info(f"Pulling image: {image_name}")
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "pull", image_name],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Failed to pull image: {result.stderr}")
            
            # Remove any existing container with the same name (cleanup from previous runs)
            container_name = f"swebench_{self.instance.instance_id}"
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30,
            )
            
            # Run container
            env_args = []
            for key, value in self.env_init.items():
                env_args.extend(["-e", f"{key}={value}"])
            
            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "-v", f"{self._temp_dir}:/workspace",
                *env_args,
                image_name,
                "tail", "-f", "/dev/null",  # Keep container running
            ]
            
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start container: {result.stderr}")
            
            self.container_id = result.stdout.strip()
            logger.info(f"Container started: {self.container_id[:12]}")
            
            # Setup repository in container
            await self._setup_repo()
            
        except Exception as e:
            await self.cleanup()
            raise RuntimeError(f"Failed to start container: {e}") from e

    async def _setup_repo(self):
        """Setup repository at base commit in container."""
        if not self.container_id:
            raise RuntimeError("Container not started")
        
        # SWE-bench official images always place the repository at /testbed
        self._repo_path = "/testbed"
        
        # Checkout base commit
        logger.info(f"Checking out base commit: {self.instance.base_commit}")
        output, exit_code = await self.execute_command(
            f"cd {self._repo_path} && git checkout -f {self.instance.base_commit}"
        )
        if exit_code != 0:
            logger.warning(f"Failed to checkout base commit: {output}")
        
        # Reset any local changes
        await self.execute_command(f"cd {self._repo_path} && git reset --hard HEAD")
        await self.execute_command(f"cd {self._repo_path} && git clean -fd")

    async def execute_command(
        self, 
        command: str, 
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
    ) -> Tuple[str, int]:
        """Execute command in container.
        
        Uses stdin to pass command to avoid Windows command line length limit (~8191 chars).
        This allows executing commands with large content (e.g., base64 encoded files).
        """
        if not self.container_id:
            raise RuntimeError("Container not started")

        exec_timeout = timeout if timeout is not None else self.timeout
        
        try:
            # Use -i (interactive) to read command from stdin
            # This bypasses Windows command line length limits
            cmd = ["docker", "exec", "-i"]
            if workdir:
                cmd.extend(["-w", workdir])
            cmd.extend([self.container_id, "bash"])
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            # Pass command through stdin
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=command.encode('utf-8')),
                timeout=exec_timeout
            )

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0

            return output, exit_code

        except asyncio.TimeoutError:
            return "Command timed out", -1
        except Exception as e:
            return f"Error executing command: {e}", -1

    async def apply_patch(self, patch_content: str) -> Tuple[bool, str]:
        """Apply a patch to the repository."""
        if not self.container_id:
            raise RuntimeError("Container not started")
        
        # Write patch to temp file in container
        patch_path = "/tmp/agent_patch.diff"
        
        # Escape patch content for shell
        escaped_patch = patch_content.replace("'", "'\\''")
        output, exit_code = await self.execute_command(
            f"echo '{escaped_patch}' > {patch_path}"
        )
        
        if exit_code != 0:
            return False, f"Failed to write patch: {output}"
        
        # Apply patch
        output, exit_code = await self.execute_command(
            f"cd {self._repo_path} && git apply --check {patch_path}"
        )
        
        if exit_code != 0:
            return False, f"Patch check failed: {output}"
        
        output, exit_code = await self.execute_command(
            f"cd {self._repo_path} && git apply {patch_path}"
        )
        
        if exit_code != 0:
            return False, f"Failed to apply patch: {output}"
        
        return True, "Patch applied successfully"

    async def run_tests(self) -> Tuple[float, Dict[str, Any]]:
        """Run tests and return reward and details.
        
        Based on official swebench harness implementation.
        """
        if not self.container_id:
            raise RuntimeError("Container not started")
        
        # Generate eval script using built-in implementation
        eval_script = make_eval_script(
            repo=self.instance.repo,
            base_commit=self.instance.base_commit,
            test_patch=self.instance.test_patch or "",
            repo_directory=self._repo_path,
            env_name="testbed",
        )
        
        # Save eval script to log for debugging
        eval_script_log = self.logs_dir / "eval.sh"
        with eval_script_log.open("w", encoding="utf-8") as f:
            f.write(eval_script)
        
        # Write eval script to container and execute
        await self.execute_command(
            f"cat > /eval.sh << 'EOF_EVAL_SCRIPT'\n{eval_script}\nEOF_EVAL_SCRIPT"
        )
        await self.execute_command("chmod +x /eval.sh")
        
        # Run eval script with extended timeout for test execution
        test_output, exit_code = await self.execute_command(
            "/bin/bash /eval.sh",
            timeout=self.timeout,
        )
        
        # Save test output to log
        test_output_log = self.logs_dir / "test_output.txt"
        with test_output_log.open("w", encoding="utf-8") as f:
            f.write(test_output)
        
        # Parse results using built-in implementation
        test_results = get_eval_tests_report(
            test_output=test_output,
            repo=self.instance.repo,
            fail_to_pass=self.instance.FAIL_TO_PASS,
            pass_to_pass=self.instance.PASS_TO_PASS,
        )
        
        # Build results dict
        results = {
            "fail_to_pass": {
                "passed": test_results["FAIL_TO_PASS"]["success"],
                "failed": test_results["FAIL_TO_PASS"]["failure"],
            },
            "pass_to_pass": {
                "passed": test_results["PASS_TO_PASS"]["success"],
                "failed": test_results["PASS_TO_PASS"]["failure"],
            },
        }
        
        # Calculate reward
        fail_to_pass_total = len(self.instance.FAIL_TO_PASS) if self.instance.FAIL_TO_PASS else 0
        fail_to_pass_success = len(results["fail_to_pass"]["passed"])
        pass_to_pass_total = len(self.instance.PASS_TO_PASS) if self.instance.PASS_TO_PASS else 0
        pass_to_pass_success = len(results["pass_to_pass"]["passed"])
        
        all_f2p_pass = fail_to_pass_success == fail_to_pass_total if fail_to_pass_total > 0 else True
        all_p2p_pass = pass_to_pass_success == pass_to_pass_total if pass_to_pass_total > 0 else True
        resolved = all_f2p_pass and all_p2p_pass
        reward = 1.0 if resolved else 0.0
        
        results["reward"] = reward
        results["summary"] = {
            "fail_to_pass": f"{fail_to_pass_success}/{fail_to_pass_total}",
            "pass_to_pass": f"{pass_to_pass_success}/{pass_to_pass_total}",
        }
        
        # Save test results to log
        test_log = self.logs_dir / "test_results.log"
        with test_log.open("w", encoding="utf-8") as f:
            f.write(f"Instance: {self.instance.instance_id}\n")
            f.write(f"Resolved: {resolved}\n")
            f.write(f"Reward: {reward}\n")
            f.write(f"FAIL_TO_PASS: {fail_to_pass_success}/{fail_to_pass_total}\n")
            f.write(f"PASS_TO_PASS: {pass_to_pass_success}/{pass_to_pass_total}\n")
            f.write(f"\nDetailed results:\n")
            f.write(f"F2P passed: {results['fail_to_pass']['passed']}\n")
            f.write(f"F2P failed: {results['fail_to_pass']['failed']}\n")
            f.write(f"P2P passed: {results['pass_to_pass']['passed']}\n")
            f.write(f"P2P failed: {results['pass_to_pass']['failed']}\n")
        
        return reward, results

    async def get_file_content(self, file_path: str) -> Tuple[str, int]:
        """Read file content from container."""
        return await self.execute_command(f"cat {file_path}")

    async def write_file(self, file_path: str, content: str) -> Tuple[bool, str]:
        """Write content to file in container."""
        # Escape content for shell
        escaped_content = content.replace("'", "'\\''")
        output, exit_code = await self.execute_command(
            f"cat > {file_path} << 'EOFMARKER'\n{content}\nEOFMARKER"
        )
        if exit_code != 0:
            return False, f"Failed to write file: {output}"
        return True, "File written successfully"

    async def list_files(self, directory: str = ".") -> Tuple[str, int]:
        """List files in directory."""
        return await self.execute_command(f"find {directory} -type f -name '*.py' | head -100")

    def get_container_id(self) -> Optional[str]:
        """Get the container ID."""
        return self.container_id

    async def cleanup(self):
        """Clean up container and temporary files."""
        if self.container_id:
            try:
                # Stop and remove container
                await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "rm", "-f", self.container_id],
                    capture_output=True,
                    timeout=30,
                )
                logger.info(f"Container removed: {self.container_id[:12]}")
            except Exception as e:
                logger.warning(f"Failed to remove container: {e}")
            finally:
                self.container_id = None
        
        if self._temp_dir and self._temp_dir.exists():
            try:
                shutil.rmtree(self._temp_dir)
            except Exception as e:
                logger.warning(f"Failed to remove temp dir: {e}")
            finally:
                self._temp_dir = None

