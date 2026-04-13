"""
Terminal-Bench 2.0 benchmark adapter.
Generate → Docker sandbox execution → test.sh verification → reward.
"""
import os
import re
import json
import glob
import subprocess
import time
from typing import List
from .base import BaseBenchmark, Task, VerifyResult

try:
    import tomllib
except ImportError:
    import tomli as tomllib

HARBOR_TASKS_DIR = "/home/xieht/.cache/harbor/tasks/packages/terminal-bench"
TASKS_JSON = "/data/xieht/terminal_bench_tasks.json"


class TerminalBench(BaseBenchmark):

    def __init__(self, tasks_file=TASKS_JSON, harbor_dir=HARBOR_TASKS_DIR,
                 agent_timeout=600, verifier_timeout=900):
        self.tasks_file = tasks_file
        self.harbor_dir = harbor_dir
        self.agent_timeout = agent_timeout
        self.verifier_timeout = verifier_timeout

    @property
    def name(self):
        return "Terminal-Bench-2.0"

    def load(self, max_tasks=None) -> List[Task]:
        raw_tasks = json.load(open(self.tasks_file))
        if max_tasks:
            raw_tasks = raw_tasks[:max_tasks]
        tasks = []
        for t in raw_tasks:
            instruction = t.get("instruction", "")
            question = (
                f"You are given a terminal/systems programming task. "
                f"Provide the complete executable solution.\n\n"
                f"Task: {instruction}\n\n"
                f"Provide all commands and code needed."
            )
            tasks.append(Task(
                task_id=t["task_id"], raw=t, question=question,
                context={"task_instruction": instruction},
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return router_output  # Keep full output; extract_commands handles it

    def _get_task_config(self, task_id):
        toml_files = glob.glob(f"{self.harbor_dir}/{task_id}/*/task.toml")
        if not toml_files:
            return None, None
        path = toml_files[0]
        with open(path, "rb") as f:
            config = tomllib.load(f)
        return config, os.path.dirname(path)

    def _extract_commands(self, answer):
        blocks = re.findall(r"```(?:bash|sh|shell)?\s*\n(.*?)```", answer, re.DOTALL)
        if blocks:
            return "\n".join(blocks)
        lines = []
        for line in answer.split("\n"):
            s = line.strip()
            if s and any(s.startswith(c) for c in [
                "sudo", "apt", "pip", "make", "gcc", "g++", "cd ", "mkdir",
                "wget", "curl", "git ", "chmod", "cp ", "mv ", "echo ", "export",
                "python", "npm", "cargo", "cmake", "tar ", "unzip", "./",
            ]):
                lines.append(s)
        return "\n".join(lines) if lines else answer

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        config, task_dir = self._get_task_config(task.task_id)
        if not config:
            return VerifyResult(task.task_id, 0.0, error=f"No config for {task.task_id}")

        env_cfg = config.get("environment", {})
        docker_image = env_cfg.get("docker_image", "")
        if not docker_image:
            return VerifyResult(task.task_id, 0.0, error="No docker_image")

        tests_dir = os.path.join(task_dir, "tests")
        if not os.path.isdir(tests_dir):
            return VerifyResult(task.task_id, 0.0, error="No tests dir")

        # Setup
        container = f"eval_{task.task_id}_{int(time.time())}"
        task_logs = os.path.join(logs_dir or "/tmp", task.task_id)
        verifier_logs = os.path.join(task_logs, "verifier")
        os.makedirs(verifier_logs, exist_ok=True)

        cpus = env_cfg.get("cpus", 1)
        mem = env_cfg.get("memory_mb", 2048)
        v_timeout = int(config.get("verifier", {}).get("timeout_sec", self.verifier_timeout))
        log = ""

        try:
            # Pull + start container
            subprocess.run(["docker", "pull", docker_image], capture_output=True, timeout=300)
            rc = subprocess.run([
                "docker", "run", "-d", "--name", container,
                "--cpus", str(cpus), "--memory", f"{mem}m",
                "-v", f"{verifier_logs}:/logs/verifier",
                docker_image, "sleep", str(v_timeout + 120),
            ], capture_output=True, text=True, timeout=60)
            if rc.returncode != 0:
                return VerifyResult(task.task_id, 0.0, error=f"docker run: {rc.stderr[:300]}")

            # Execute solution
            commands = self._extract_commands(answer)
            script_path = os.path.join(task_logs, "solution.sh")
            with open(script_path, "w") as f:
                f.write(f"#!/bin/bash\nset -e\n{commands}\n")
            subprocess.run(["docker", "cp", script_path, f"{container}:/tmp/solution.sh"],
                           capture_output=True, timeout=30)
            ep = subprocess.run(
                ["docker", "exec", container, "bash", "/tmp/solution.sh"],
                capture_output=True, text=True,
                timeout=min(self.agent_timeout, 600),
            )
            log += f"AGENT stdout:\n{ep.stdout[-1000:]}\nstderr:\n{ep.stderr[-1000:]}\n"

            # Copy tests + run verification
            subprocess.run(["docker", "cp", tests_dir, f"{container}:/tests"],
                           capture_output=True, timeout=30)
            subprocess.run(["docker", "exec", container, "mkdir", "-p", "/logs/verifier"],
                           capture_output=True, timeout=10)
            tp = subprocess.run(
                ["docker", "exec", container, "bash", "/tests/test.sh"],
                capture_output=True, text=True, timeout=v_timeout,
            )
            log += f"VERIFIER stdout:\n{tp.stdout[-1000:]}\nstderr:\n{tp.stderr[-1000:]}\n"

            # Read reward
            reward_file = os.path.join(verifier_logs, "reward.txt")
            if not os.path.exists(reward_file):
                subprocess.run(
                    ["docker", "cp", f"{container}:/logs/verifier/reward.txt", reward_file],
                    capture_output=True, timeout=10,
                )
            if os.path.exists(reward_file):
                content = open(reward_file).read().strip()
                try:
                    reward = float(content)
                except ValueError:
                    reward = 0.0
            else:
                reward = 0.0

            return VerifyResult(task.task_id, reward, log=log)

        except subprocess.TimeoutExpired:
            return VerifyResult(task.task_id, 0.0, error="Timeout", log=log)
        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log)
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
