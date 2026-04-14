"""
Terminal-Bench 2.0 benchmark adapter.
Supports two modes:
1. One-shot: generate solution text → extract commands → execute in Docker → verify
2. Interactive: router ↔ Docker multi-turn loop (execute_shell/execute_python via real Docker)
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

# Skills that should execute in Docker rather than call LLM API
EXEC_SKILLS = {"execute_shell", "execute_python"}


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
        return router_output

    def _get_task_config(self, task_id):
        toml_files = glob.glob(f"{self.harbor_dir}/{task_id}/*/task.toml")
        if not toml_files:
            return None, None
        path = toml_files[0]
        with open(path, "rb") as f:
            config = tomllib.load(f)
        return config, os.path.dirname(path)

    def _extract_commands(self, answer):
        """Extract executable bash from any router output format."""
        text = answer

        # Router-R1: extract from <information> blocks
        info_blocks = re.findall(r"<information>(.*?)</information>", text, re.DOTALL)
        if info_blocks:
            text = "\n".join(info_blocks)

        # SkillRouter-SFT: extract from <obs> blocks + <final_answer>
        obs_blocks = re.findall(r"<obs[^>]*>(.*?)</obs>", text, re.DOTALL)
        if obs_blocks:
            final = re.search(r"<final_answer>(.*?)</final_answer>", answer, re.DOTALL)
            text = "\n".join(obs_blocks)
            if final:
                text += "\n" + final.group(1)

        # Strip XML tags
        text = re.sub(r"</?(?:think|search|answer|plan|route|subtask|verify|final_answer|obs|information)[^>]*>", "", text)
        text = re.sub(r"\[/?(?:ASSISTANT|TOOL)\]", "", text)

        # Extract bash blocks
        blocks = re.findall(r"```(?:bash|sh|shell)?\s*\n(.*?)```", text, re.DOTALL)
        if blocks:
            return "\n".join(blocks)

        # Extract python blocks
        py_blocks = re.findall(r"```(?:python)\s*\n(.*?)```", text, re.DOTALL)
        if py_blocks:
            return "\n".join(f"python3 << 'PYEOF'\n{pb}\nPYEOF" for pb in py_blocks)

        # Fallback: extract command-like lines
        lines = []
        for line in text.split("\n"):
            s = line.strip()
            if s and not s.startswith("#") and any(s.startswith(c) for c in [
                "sudo", "apt", "pip", "make", "gcc", "g++", "cd ", "mkdir",
                "wget", "curl", "git ", "chmod", "cp ", "mv ", "echo ", "export",
                "python", "npm", "cargo", "cmake", "tar ", "unzip", "sed ",
                "cat ", "tee ", "source", "docker", "dnf", "yum", "./",
            ]):
                lines.append(s)
        return "\n".join(lines) if lines else text

    # ─────────────────────────────────────────────────────────────
    # Interactive mode: router ↔ Docker multi-turn execution
    # ─────────────────────────────────────────────────────────────

    def interactive_verify(self, task: Task, router, logs_dir=None) -> VerifyResult:
        """
        Multi-turn interactive evaluation:
        1. Start Docker container from task image
        2. Router generates <plan> + <route>
        3. For execute_shell/execute_python skills: run command in Docker, return real stdout as <obs>
        4. For other skills: call LLM API as usual
        5. Router sees <obs>, generates <verify> + <final_answer> or next <plan>
        6. Repeat until done or max rounds
        7. Run test.sh to verify
        """
        from ..routers.base import RouteResult
        from ..config import COST_PER_M, EVAL_MAX_TOKENS, SUB_AGENT_TEMP, resolve_model
        import openai

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

        container = f"eval_{task.task_id}_{int(time.time())}"
        task_logs = os.path.join(logs_dir or "/tmp", task.task_id)
        verifier_logs = os.path.join(task_logs, "verifier")
        os.makedirs(verifier_logs, exist_ok=True)

        cpus = env_cfg.get("cpus", 1)
        mem = env_cfg.get("memory_mb", 2048)
        v_timeout = int(config.get("verifier", {}).get("timeout_sec", self.verifier_timeout))
        log = ""
        total_cost = 0.0

        # Regex for parsing SkillRouter schema
        ROUTE_RE = re.compile(
            r'<route[^>]*model="([^"]+)"[^>]*skill="([^"]+)"[^>]*>(.*?)</route>', re.DOTALL)
        FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)

        try:
            # Start container
            subprocess.run(["docker", "pull", docker_image], capture_output=True, timeout=300)
            rc = subprocess.run([
                "docker", "run", "-d", "--name", container,
                "--cpus", str(cpus), "--memory", f"{mem}m",
                "-v", f"{verifier_logs}:/logs/verifier",
                docker_image, "sleep", str(v_timeout + 300),
            ], capture_output=True, text=True, timeout=60)
            if rc.returncode != 0:
                return VerifyResult(task.task_id, 0.0, error=f"docker run: {rc.stderr[:300]}")

            # Build initial prompt
            system_prompt = getattr(router, 'system_prompt', '')
            instruction = task.context.get("task_instruction", task.question)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": instruction})

            # Multi-turn loop
            max_rounds = 3
            for round_idx in range(max_rounds):
                # Get router output
                try:
                    resp = router.local.chat.completions.create(
                        model=router.model_name,
                        messages=messages,
                        temperature=0.0,
                        max_tokens=2048,
                    )
                    assistant_text = resp.choices[0].message.content or ""
                except Exception as e:
                    log += f"\n[ROUTER ERROR round {round_idx}: {e}]"
                    break

                log += f"\n[ROUND {round_idx+1} ASSISTANT]\n{assistant_text[:500]}\n"
                messages.append({"role": "assistant", "content": assistant_text})

                # Check for final_answer (lazy mode or after verify pass)
                if FINAL_RE.search(assistant_text):
                    break

                # Parse routes
                routes = ROUTE_RE.findall(assistant_text)
                if not routes:
                    break

                obs_parts = []
                for model, skill, query in routes:
                    if skill in EXEC_SKILLS:
                        # Execute in Docker directly
                        cmd = query.strip()
                        if skill == "execute_python":
                            script = f"python3 << 'PYEOF'\n{cmd}\nPYEOF"
                            cmd = script
                        try:
                            ep = subprocess.run(
                                ["docker", "exec", container, "bash", "-c", cmd],
                                capture_output=True, text=True, timeout=120,
                            )
                            result = ep.stdout[-1500:] if ep.stdout else ""
                            if ep.stderr:
                                result += f"\nSTDERR: {ep.stderr[-500:]}"
                            if ep.returncode != 0:
                                result += f"\n[exit code: {ep.returncode}]"
                        except subprocess.TimeoutExpired:
                            result = "[command timed out after 120s]"
                        obs_parts.append(result)
                    else:
                        # For non-execution skills: call LLM, then ALSO execute
                        # any bash/python commands found in the LLM response
                        actual_model = resolve_model(model)
                        try:
                            sr = router.api.chat.completions.create(
                                model=actual_model,
                                messages=[{"role": "user", "content": (
                                    f"Task: {instruction}\n"
                                    f"Sub-task: {query}\n"
                                    f"Provide executable bash commands. Use ```bash ... ``` blocks."
                                )}],
                                temperature=SUB_AGENT_TEMP,
                                max_tokens=EVAL_MAX_TOKENS,
                            )
                            llm_response = sr.choices[0].message.content or ""
                            toks = getattr(sr.usage, "completion_tokens", 0) or 0
                            total_cost += COST_PER_M.get(model, 10.0) * max(toks, 1) / 1e6
                        except Exception as e:
                            llm_response = f"API error: {str(e)[:200]}"

                        # Extract and execute commands from LLM response
                        cmds = self._extract_commands(llm_response)
                        if cmds and cmds != llm_response:
                            try:
                                ep = subprocess.run(
                                    ["docker", "exec", container, "bash", "-c", cmds],
                                    capture_output=True, text=True, timeout=120,
                                )
                                exec_result = ep.stdout[-800:] if ep.stdout else ""
                                if ep.stderr:
                                    exec_result += f"\nSTDERR: {ep.stderr[-300:]}"
                                result = f"{llm_response[:500]}\n[EXECUTED]\n{exec_result}"
                            except subprocess.TimeoutExpired:
                                result = f"{llm_response[:500]}\n[EXECUTED] timeout"
                        else:
                            result = llm_response
                        obs_parts.append(result)

                # Build obs and feed back to router
                obs_content = "\n".join(
                    f'<obs subtask="{i+1}">{obs}</obs>' for i, obs in enumerate(obs_parts)
                )
                log += f"\n[ROUND {round_idx+1} OBS]\n{obs_content[:500]}\n"
                messages.append({"role": "user", "content": obs_content})

            # Run verification test.sh
            subprocess.run(["docker", "cp", tests_dir, f"{container}:/tests"],
                           capture_output=True, timeout=30)
            subprocess.run(["docker", "exec", container, "mkdir", "-p", "/logs/verifier"],
                           capture_output=True, timeout=10)
            tp = subprocess.run(
                ["docker", "exec", container, "bash", "/tests/test.sh"],
                capture_output=True, text=True, timeout=v_timeout,
            )
            log += f"\n[VERIFIER]\nstdout: {tp.stdout[-500:]}\nstderr: {tp.stderr[-500:]}\n"

            # Read reward
            reward_file = os.path.join(verifier_logs, "reward.txt")
            if not os.path.exists(reward_file):
                subprocess.run(
                    ["docker", "cp", f"{container}:/logs/verifier/reward.txt", reward_file],
                    capture_output=True, timeout=10,
                )
            if os.path.exists(reward_file):
                try:
                    reward = float(open(reward_file).read().strip())
                except ValueError:
                    reward = 0.0
            else:
                reward = 0.0

            return VerifyResult(task.task_id, reward, log=log[:3000])

        except subprocess.TimeoutExpired:
            return VerifyResult(task.task_id, 0.0, error="Timeout", log=log[:3000])
        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log[:3000])
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)

    # ─────────────────────────────────────────────────────────────
    # One-shot mode (for non-interactive routers like Direct/Oracle)
    # ─────────────────────────────────────────────────────────────

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

        container = f"eval_{task.task_id}_{int(time.time())}"
        task_logs = os.path.join(logs_dir or "/tmp", task.task_id)
        verifier_logs = os.path.join(task_logs, "verifier")
        os.makedirs(verifier_logs, exist_ok=True)

        cpus = env_cfg.get("cpus", 1)
        mem = env_cfg.get("memory_mb", 2048)
        v_timeout = int(config.get("verifier", {}).get("timeout_sec", self.verifier_timeout))
        log = ""

        try:
            subprocess.run(["docker", "pull", docker_image], capture_output=True, timeout=300)
            rc = subprocess.run([
                "docker", "run", "-d", "--name", container,
                "--cpus", str(cpus), "--memory", f"{mem}m",
                "-v", f"{verifier_logs}:/logs/verifier",
                docker_image, "sleep", str(v_timeout + 120),
            ], capture_output=True, text=True, timeout=60)
            if rc.returncode != 0:
                return VerifyResult(task.task_id, 0.0, error=f"docker run: {rc.stderr[:300]}")

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

            subprocess.run(["docker", "cp", tests_dir, f"{container}:/tests"],
                           capture_output=True, timeout=30)
            subprocess.run(["docker", "exec", container, "mkdir", "-p", "/logs/verifier"],
                           capture_output=True, timeout=10)
            tp = subprocess.run(
                ["docker", "exec", container, "bash", "/tests/test.sh"],
                capture_output=True, text=True, timeout=v_timeout,
            )
            log += f"VERIFIER stdout:\n{tp.stdout[-1000:]}\nstderr:\n{tp.stderr[-1000:]}\n"

            reward_file = os.path.join(verifier_logs, "reward.txt")
            if not os.path.exists(reward_file):
                subprocess.run(
                    ["docker", "cp", f"{container}:/logs/verifier/reward.txt", reward_file],
                    capture_output=True, timeout=10,
                )
            if os.path.exists(reward_file):
                try:
                    reward = float(open(reward_file).read().strip())
                except ValueError:
                    reward = 0.0
            else:
                reward = 0.0

            return VerifyResult(task.task_id, reward, log=log[:3000])

        except subprocess.TimeoutExpired:
            return VerifyResult(task.task_id, 0.0, error="Timeout", log=log[:3000])
        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log[:3000])
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
