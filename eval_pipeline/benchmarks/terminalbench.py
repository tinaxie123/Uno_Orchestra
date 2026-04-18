"""
Terminal-Bench 2.0 benchmark adapter.

Architecture (following AOrchestra):
  TerminalBench (BaseBenchmark)
    └── interactive_verify() — multi-turn: router ↔ Docker
          └── DockerExecutor — container lifecycle, exec, test, cleanup
                └── DockerComposeManager — compose up/down, signal handling

Two modes:
  1. Interactive: router ↔ Docker multi-turn with real shell feedback (primary)
  2. One-shot: router generates commands → execute → verify (fallback)
"""
import asyncio
import os
import re
import json
import glob
import subprocess
import time
import logging
from pathlib import Path
from typing import List, Optional

from .base import BaseBenchmark, Task, VerifyResult

try:
    import tomllib
except ImportError:
    import tomli as tomllib

logger = logging.getLogger(__name__)

HARBOR_TASKS_DIR = "/home/xieht/.cache/harbor/tasks/packages/terminal-bench"
COMPOSE_YAML = Path(__file__).parent.parent / "executors" / "docker-compose-build.yaml"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# System prompt for interactive mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INTERACTIVE_SYSTEM_PROMPT = """\
You are completing a terminal/systems task inside a Docker container.
Execute shell commands step by step to accomplish the task.

Respond with EXACTLY this format each turn:

DISCUSSION
<your step-by-step reasoning>
COMMAND
<single shell command>

When the task is complete:

DISCUSSION
<summary of what you accomplished>
COMMAND
finish

RULES:
- ONE command per turn. Wait for output before the next step.
- You are root. The working directory is set by the container.
- For package installs: use DEBIAN_FRONTEND=noninteractive and -y flags.
- If dpkg lock error: run `kill $(lsof -t /var/lib/dpkg/lock-frontend) 2>/dev/null; rm -f /var/lib/dpkg/lock*` first.
- Before installing, check if tools exist: `which <tool>` or `command -v <tool>`.
- Prefer pip/conda over apt when possible (faster, fewer lock issues).
- Long commands: chain with && to avoid partial failure.
- If a command times out, try a simpler alternative.
"""

INTERACTIVE_SUBAGENT_PROMPT = """\
You are an orchestrator completing a terminal/systems task inside a Docker container.
You can either execute commands directly OR delegate to a specialized LLM for help.

Each turn, respond with ONE of these formats:

Option A — Execute a command yourself:
DISCUSSION
<reasoning>
COMMAND
<single shell command>

Option B — Ask a specialized LLM for help:
DISCUSSION
<reasoning about which model to use and why>
COMMAND
<search> ModelName:Your question or request </search>

Available models (input/output $/1M tokens):
Gemini-2.5-Flash-Lite($0.10/$0.40) Gemini-2.5-Flash($0.30/$2.50) Kimi-K2.5($0.35/$2.50)
Gemini-3-Flash-Preview($0.50/$3) Claude-Haiku-4.5($1/$5) GPT-5.3-Codex($1.75/$14)
GPT-5.4($2.50/$15) Claude-Sonnet-4.6($3/$15) Claude-Opus-4.6($5/$25)

The LLM response will appear as [API ...]: <response>. You can then use that to write commands.

When done:
DISCUSSION
<summary>
COMMAND
finish

RULES:
- ONE action per turn (either a shell command or a <search> call).
- Use cheap models for simple questions, expensive ones for complex code/reasoning.
- You are root inside the container. Use DEBIAN_FRONTEND=noninteractive for apt.
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TerminalBench benchmark
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TerminalBench(BaseBenchmark):

    def __init__(self, harbor_dir=HARBOR_TASKS_DIR,
                 max_steps=30, docker_timeout=600, verifier_timeout=900):
        self.harbor_dir = harbor_dir
        self.max_steps = max_steps
        self.docker_timeout = docker_timeout
        self.verifier_timeout = verifier_timeout
        # Lazy-init docker manager (only when interactive mode is used)
        self._docker_manager = None

    @property
    def name(self):
        return "Terminal-Bench-2.0"

    def _get_docker_manager(self):
        if self._docker_manager is None:
            from ..executors import DockerComposeManager
            self._docker_manager = DockerComposeManager(COMPOSE_YAML)
        return self._docker_manager

    # ─── Task loading ───────────────────────────────────────────

    def load(self, max_tasks=None) -> List[Task]:
        """Load tasks directly from harbor cache (instruction.md + task.toml)."""
        tasks = []
        for task_dir_name in sorted(os.listdir(self.harbor_dir)):
            task_base = os.path.join(self.harbor_dir, task_dir_name)
            if not os.path.isdir(task_base):
                continue
            sub_dirs = glob.glob(os.path.join(task_base, "*/task.toml"))
            if not sub_dirs:
                continue
            task_dir = os.path.dirname(sub_dirs[0])

            # Read instruction
            instr_path = os.path.join(task_dir, "instruction.md")
            instruction = ""
            if os.path.exists(instr_path):
                instruction = open(instr_path).read().strip()
            if not instruction:
                continue

            # Read task.toml
            with open(os.path.join(task_dir, "task.toml"), "rb") as f:
                config = tomllib.load(f)

            tasks.append(Task(
                task_id=task_dir_name,
                raw={"config": config, "task_dir": task_dir},
                question=f"Task: {instruction}",
                context={"task_instruction": instruction},
            ))

            if max_tasks and len(tasks) >= max_tasks:
                break
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return router_output

    # ─── Interactive mode: router ↔ Docker (AOrchestra style) ──

    def interactive_verify(self, task: Task, router, logs_dir=None) -> VerifyResult:
        """
        Multi-turn interactive evaluation using AOrchestra's executor:
        1. Start container via docker-compose (supports prebuilt image + Dockerfile build)
        2. Router outputs DISCUSSION + COMMAND
        3. Command executes in container → real output returned
        4. Repeat until 'finish' or max_steps
        5. Run test.sh via executor → read reward
        """
        # Create a new event loop for this thread (ThreadPoolExecutor doesn't have one)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._async_interactive_verify(task, router, logs_dir)
            )
        finally:
            loop.close()

    async def _async_interactive_verify(self, task: Task, router, logs_dir=None) -> VerifyResult:
        from ..executors import DockerExecutor

        # Parse task config
        config = task.raw.get("config", {})
        task_dir = Path(task.raw.get("task_dir", ""))
        if not config:
            # Fallback: load from harbor
            config, task_dir_str = self._get_task_config(task.task_id)
            if not config:
                return VerifyResult(task.task_id, 0.0, error=f"No config for {task.task_id}")
            task_dir = Path(task_dir_str)

        # Setup log directories
        task_logs = Path(logs_dir or "/tmp") / task.task_id
        verifier_logs = task_logs / "verifier"
        agent_logs = task_logs / "agent"
        verifier_logs.mkdir(parents=True, exist_ok=True)
        agent_logs.mkdir(parents=True, exist_ok=True)

        log = ""

        # Create executor (AOrchestra's DockerExecutor)
        executor = DockerExecutor(
            task_id=task.task_id,
            task_dir=task_dir,
            task_config=config,
            verifier_logs_dir=verifier_logs,
            agent_logs_dir=agent_logs,
            docker_manager=self._get_docker_manager(),
            docker_timeout=self.docker_timeout,
        )

        CMD_RE = re.compile(r"COMMAND\s*\n(.+?)(?:\n\n|\Z)", re.DOTALL)

        try:
            # Start container (docker-compose: supports Dockerfile build + prebuilt)
            await executor.start_container()

            # Build messages
            instruction = task.context.get("task_instruction", task.question)
            messages = [
                {"role": "system", "content": INTERACTIVE_SYSTEM_PROMPT},
                {"role": "user", "content": f"## Task\n{instruction}"},
            ]

            # Multi-turn loop
            for step in range(self.max_steps):
                # Router generates next action
                try:
                    resp = router.local.chat.completions.create(
                        model=router.model_name,
                        messages=messages,
                        temperature=0.0,
                        max_tokens=2048,
                    )
                    assistant_text = resp.choices[0].message.content or ""
                except Exception as e:
                    log += f"\n[ROUTER ERROR step {step}: {e}]"
                    break

                messages.append({"role": "assistant", "content": assistant_text})
                log += f"\n[STEP {step+1}] ASSISTANT:\n{assistant_text[:500]}\n"

                # Parse COMMAND
                cmd_match = CMD_RE.search(assistant_text)
                if not cmd_match:
                    log += "[NO COMMAND FOUND]\n"
                    break

                command = cmd_match.group(1).strip().split("\n")[0].strip()

                if command.lower() == "finish":
                    log += "[FINISH]\n"
                    break

                # Execute in Docker via executor (async, proper timeout)
                output, exit_code = await executor.execute_command(command, timeout=300)
                output = output[-2000:]  # truncate

                obs = f"[Step {step+1}/{self.max_steps}] exit_code={exit_code}\n{output}"
                log += f"[STEP {step+1}] CMD: {command}\n[OUTPUT] {output[:500]}\n"
                messages.append({"role": "user", "content": obs})

                # Log command to agent log file
                cmd_log = agent_logs / "commands.log"
                with cmd_log.open("a") as f:
                    f.write(f"[Step {step+1}] {command}\nExit: {exit_code}\n{output[:1000]}\n{'-'*60}\n")

            # Run verification tests via executor
            reward = await executor.run_tests()
            log += f"\n[VERIFIER] reward={reward}\n"

            # Save trace
            with (task_logs / "trace.log").open("w") as f:
                f.write(log)

            return VerifyResult(task.task_id, reward, log=log[-3000:])

        except Exception as e:
            logger.error(f"[{task.task_id}] interactive_verify failed: {e}")
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log[-3000:])
        finally:
            try:
                await executor.cleanup()
            except Exception as e:
                logger.warning(f"[{task.task_id}] cleanup failed: {e}")

    # ─── One-shot mode (fallback for non-interactive routers) ──

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        """One-shot: extract commands from answer → execute in Docker → run test.sh."""
        config = task.raw.get("config", {})
        task_dir_str = task.raw.get("task_dir", "")
        if not config:
            config, task_dir_str = self._get_task_config(task.task_id)
        if not config:
            return VerifyResult(task.task_id, 0.0, error=f"No config for {task.task_id}")

        env_cfg = config.get("environment", {})
        docker_image = env_cfg.get("docker_image", "")
        if not docker_image:
            return VerifyResult(task.task_id, 0.0, error="No docker_image")

        tests_dir = os.path.join(task_dir_str, "tests")
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
                timeout=min(self.docker_timeout, 600),
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

    # ─── Helpers ───────────────────────────────────────────────

    def _get_task_config(self, task_id):
        toml_files = glob.glob(f"{self.harbor_dir}/{task_id}/*/task.toml")
        if not toml_files:
            return None, None
        path = toml_files[0]
        with open(path, "rb") as f:
            config = tomllib.load(f)
        return config, os.path.dirname(path)

    def _extract_commands(self, answer):
        """Extract executable bash from router output (for one-shot mode)."""
        text = answer
        text = re.sub(r"</?(?:think|search|answer|plan|route|subtask|verify|final_answer|obs|information)[^>]*>", "", text)
        text = re.sub(r"\[/?(?:ASSISTANT|TOOL)\]", "", text)

        blocks = re.findall(r"```(?:bash|sh|shell)?\s*\n(.*?)```", text, re.DOTALL)
        if blocks:
            return "\n".join(blocks)

        py_blocks = re.findall(r"```(?:python)\s*\n(.*?)```", text, re.DOTALL)
        if py_blocks:
            return "\n".join(f"python3 << 'PYEOF'\n{pb}\nPYEOF" for pb in py_blocks)

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

    # ─── Pipeline mode: planner → router → sub-agent → Docker ──

    def pipeline_verify(self, task: Task, args, pools, logs_dir=None) -> VerifyResult:
        """
        Full pipeline evaluation (our method):
        1. Planner (local) decomposes task into subtasks
        2. Router (local) selects API model + skill per subtask
        3. Sub-agent (API) generates solution/commands
        4. Extract commands → execute in Docker
        5. Run test.sh → reward
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._async_pipeline_verify(task, args, pools, logs_dir)
            )
        finally:
            loop.close()

    async def _async_pipeline_verify(self, task, args, pools, logs_dir=None):
        """
        Multi-round pipeline: planner decomposes → router selects model →
        sub-agent generates commands → Docker executes → output feeds back to planner.
        Repeats until planner finishes or max_rounds.
        """
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        from scripts.data.router import aroute_subtask
        from scripts.data.planner import arun_planner
        from openai import AsyncOpenAI
        from ..executors import DockerExecutor

        config = task.raw.get("config", {})
        task_dir = Path(task.raw.get("task_dir", ""))
        if not config:
            config, task_dir_str = self._get_task_config(task.task_id)
            if not config:
                return VerifyResult(task.task_id, 0.0, error=f"No config for {task.task_id}")
            task_dir = Path(task_dir_str)

        task_logs = Path(logs_dir or "/tmp") / task.task_id
        verifier_logs = task_logs / "verifier"
        agent_logs = task_logs / "agent"
        verifier_logs.mkdir(parents=True, exist_ok=True)
        agent_logs.mkdir(parents=True, exist_ok=True)

        log = ""
        models_used = []
        skills_used = []

        # Start Docker container first
        executor = DockerExecutor(
            task_id=task.task_id,
            task_dir=task_dir,
            task_config=config,
            verifier_logs_dir=verifier_logs,
            agent_logs_dir=agent_logs,
            docker_manager=self._get_docker_manager(),
            docker_timeout=self.docker_timeout,
        )

        sub_client = AsyncOpenAI(
            base_url=args.api_base, api_key=args.api_key, timeout=60,
        )
        _extra = {"enable_thinking": False}

        try:
            await executor.start_container()
            instruction = task.context.get("task_instruction", task.question)
            env_feedback = ""  # accumulate Docker output for planner context
            max_rounds = 10

            # Define execute_subtask: router picks model → sub-agent responds → extract commands → Docker exec
            async def execute_subtask(subtask_instruction: str, task_id: str) -> str:
                nonlocal env_feedback
                # Same as arun_agent: router selects model + skill
                selected_model, selected_skill = await aroute_subtask(
                    instruction=subtask_instruction,
                    model=args.local_model or "Qwen/Qwen2.5-7B-Instruct",
                    api_base=args.local_base, api_key="none",
                    pools=pools, temperature=0.3,
                )
                models_used.append(selected_model)
                skills_used.append(selected_skill)

                # Same as arun_agent: sub-agent receives instruction directly
                try:
                    resp = await sub_client.chat.completions.create(
                        model=selected_model,
                        messages=[{"role": "user", "content": subtask_instruction}],
                        temperature=0.1, max_tokens=1024,
                        extra_body=_extra,
                    )
                    sub_response = resp.choices[0].message.content.strip()
                except Exception as e:
                    sub_response = f"Error: {e}"

                # Extra step vs arun_agent: extract commands and execute in Docker
                commands = self._extract_commands(sub_response)
                if commands.strip():
                    output, exit_code = await executor.execute_command(commands, timeout=300)
                    output = output[-2000:]
                    result = f"[routed to {selected_model} / {selected_skill}]\n{sub_response}\n[Docker exit={exit_code}]\n{output}"
                else:
                    result = f"[routed to {selected_model} / {selected_skill}]\n{sub_response}"

                env_feedback += f"\n{result[-1000:]}"
                return result

            # Terminal-Bench specific planner prompt
            tb_planner_prompt = (
                "You are an orchestrator completing a terminal/systems task inside a Docker container.\n"
                "Your sub-agents will execute shell commands in the container.\n\n"
                "Tools:\n"
                "- plan_subtask(instruction, task_id): delegate to a specialist who will run commands in Docker.\n"
                "  The instruction MUST describe what shell commands to run, what packages to install,\n"
                "  what files to create/edit, etc. Be specific and actionable.\n"
                "- finish(answer): call when all work is done. The answer field can be 'done' or a brief summary.\n\n"
                "Rules:\n"
                "1. Break the task into concrete subtasks: install dependencies, write code, configure services, test.\n"
                "2. Each subtask instruction must be self-contained with exact commands or code.\n"
                "3. Include bash commands in the instruction, e.g. 'Run: apt-get install -y nginx && nginx -v'\n"
                "4. After sub-agents complete, call finish('done').\n"
                "5. You are root in the container. Use DEBIAN_FRONTEND=noninteractive for apt.\n"
            )

            # Run planner with Docker-integrated subtask execution
            planner_result = await arun_planner(
                question=instruction,
                model=args.local_model or "Qwen/Qwen2.5-7B-Instruct",
                api_base=args.local_base, api_key="none",
                execute_subtask_fn=execute_subtask,
                temperature=0.7,
                system_prompt=tb_planner_prompt,
            )

            log += f"[PIPELINE] complete={planner_result.get('complete')} models={models_used} skills={skills_used}\n"
            log += f"[ANSWER] {str(planner_result.get('answer',''))[:300]}\n"

            # Save trajectory
            with (task_logs / "pipeline_result.json").open("w") as f:
                import json
                json.dump({
                    "task_id": task.task_id,
                    "result": planner_result,
                    "models_used": models_used,
                    "skills_used": skills_used,
                    "log": log,
                }, f, indent=2, default=str)

            # Run tests
            reward = await executor.run_tests()
            log += f"[REWARD] {reward}\n"

            with (task_logs / "trace.log").open("w") as f:
                f.write(log)

            return VerifyResult(task.task_id, reward, log=log[-3000:])

        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log[-3000:])
        finally:
            try:
                await executor.cleanup()
            except Exception:
                pass
