"""
Terminal-Bench 2.0 — Planner + SubAgent pipeline.

Two levels of agents, both our own code:

  Planner (``router.chat_completions``)
    → decides ``delegate_task(worker_model, instruction)`` or ``submit(reason)``

  SubAgent (``agent_system.agents.subagent.SubAgent``)
    → runs multi-turn shell commands inside the Docker container,
      observes output, reports a structured status back to the Planner

The planner's view is a chat-completions call with two OpenAI tools:
``delegate_task`` and ``submit``. Routers that participate inherit the default
``BaseRouter.chat_completions`` (Direct, Oracle, Random) or override it with
their own orchestration (PlannerRouter / SkillRouterSFT). When the planner
calls ``submit`` — or the attempt budget is exhausted — we run the container's
``test.sh`` via ``DockerExecutor.run_tests()`` and read the reward file.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseBenchmark, Task, VerifyResult

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

logger = logging.getLogger(__name__)

HARBOR_TASKS_DIR = "/home/xieht/.cache/harbor/tasks/packages/terminal-bench"
COMPOSE_YAML = (
    Path(__file__).parent.parent / "executors" / "docker-compose-build.yaml"
)


# ----------------------------------------------------------------------
# Planner-side prompt and tool definitions
# ----------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """\
You are the Planner for a Docker-based terminal task. You do NOT execute shell
commands directly. Instead, you delegate work to a worker sub-agent that runs
inside a persistent Docker container.

## Tools
- delegate_task(worker_model, instruction)
    Delegate a concrete sub-task to the given worker model. The worker runs
    shell commands in the container (state persists across delegations), then
    returns a structured report: status (done/partial/error), what it did,
    any issues.
- submit(reason)
    Declare the whole task complete. The harness runs the task's test.sh;
    the reward file decides pass/fail.

## Rules
- Each delegate_task consumes one attempt. The container persists, so later
  delegations see the previous worker's changes.
- Start by delegating a concrete subtask, not by describing the whole task.
- After the worker returns `status=done`, inspect its `completed` list and
  `issues`. If it really addressed every requirement, call `submit`; if not,
  delegate another subtask with explicit instructions for what is missing.
- You are root in the container. Ubuntu + apt + pip available. Use
  DEBIAN_FRONTEND=noninteractive and -y for any apt installs.
- Prefer small, verifiable delegations over one monolithic "do everything".
"""

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Delegate a concrete sub-task to a worker model that runs shell "
                "commands in the shared Docker container."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_model": {
                        "type": "string",
                        "description": (
                            "Worker model id, e.g. 'Qwen/Qwen2.5-7B-Instruct', "
                            "'claude-opus-4-6', 'gpt-5.3-codex'."
                        ),
                    },
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Self-contained natural-language instructions for the "
                            "worker. Include every detail the worker needs; it "
                            "cannot see the original task."
                        ),
                    },
                },
                "required": ["worker_model", "instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": (
                "Declare the task complete. Runs the container's test.sh and "
                "finishes this trial with the resulting reward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief justification for why the task is complete.",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]


def _budget_note(attempt_idx: int, max_attempts: int) -> str:
    remaining = max_attempts - attempt_idx
    if remaining <= 2:
        return f"🚨 CRITICAL: Only {remaining} attempt(s) left — submit now if nearly done."
    if remaining <= 4:
        return f"⚠️ Warning: {remaining} attempts remaining — plan carefully."
    return f"Budget: {remaining}/{max_attempts} attempts remaining."


# ----------------------------------------------------------------------
# Benchmark
# ----------------------------------------------------------------------


class TerminalBench(BaseBenchmark):

    def __init__(
        self,
        harbor_dir: str = HARBOR_TASKS_DIR,
        max_attempts: int = 8,
        subagent_max_steps: int = 20,
        subagent_cmd_timeout: int = 300,
        docker_timeout: int = 600,
        verifier_timeout: int = 900,
    ):
        self.harbor_dir = harbor_dir
        self.max_attempts = max_attempts
        self.subagent_max_steps = subagent_max_steps
        self.subagent_cmd_timeout = subagent_cmd_timeout
        self.docker_timeout = docker_timeout
        self.verifier_timeout = verifier_timeout
        self._docker_manager = None

    @property
    def name(self):
        return "Terminal-Bench-2.0"

    def _get_docker_manager(self):
        if self._docker_manager is None:
            from ..executors import DockerComposeManager
            self._docker_manager = DockerComposeManager(COMPOSE_YAML)
        return self._docker_manager

    # ----- task loading ------------------------------------------------

    def load(self, max_tasks: Optional[int] = None) -> List[Task]:
        tasks: List[Task] = []
        for name in sorted(os.listdir(self.harbor_dir)):
            base = os.path.join(self.harbor_dir, name)
            if not os.path.isdir(base):
                continue
            tomls = glob.glob(os.path.join(base, "*/task.toml"))
            if not tomls:
                continue
            task_dir = os.path.dirname(tomls[0])
            instr_path = os.path.join(task_dir, "instruction.md")
            instruction = ""
            if os.path.exists(instr_path):
                instruction = open(instr_path).read().strip()
            if not instruction:
                continue
            with open(os.path.join(task_dir, "task.toml"), "rb") as f:
                config = tomllib.load(f)
            tasks.append(
                Task(
                    task_id=name,
                    raw={"config": config, "task_dir": task_dir},
                    question=f"Task: {instruction}",
                    context={"task_instruction": instruction},
                )
            )
            if max_tasks and len(tasks) >= max_tasks:
                break
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return router_output  # unused — interactive pipeline

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        raise NotImplementedError(
            "TerminalBench is interactive; use run_interactive(task, router, ...)"
        )

    # ----- main interactive pipeline -----------------------------------

    def run_interactive(
        self,
        task: Task,
        router,
        worker_pool: Optional[List[str]] = None,
        subagent_api_base: Optional[str] = None,
        subagent_api_key: str = "EMPTY",
        logs_dir: Optional[str] = None,
    ) -> VerifyResult:
        """Run Planner (``router``) + SubAgent(s) against ``task``.

        Args:
            task: A Task from ``self.load()``.
            router: A BaseRouter with ``chat_completions(messages, tools)``.
            worker_pool: Worker models the Planner may delegate to. If the
                Planner picks a model outside this list, we substitute the
                first element (keeps baselines honest). If None, any model is
                accepted.
            subagent_api_base: Base URL for the SubAgent's worker LLM. Defaults
                to the router's own ``api_base`` if available, else the config
                default.
            subagent_api_key: API key for the SubAgent worker LLM.
            logs_dir: Where to save trajectory + commands log.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._run_async(task, router, worker_pool, subagent_api_base, subagent_api_key, logs_dir)
            )
        finally:
            loop.close()

    async def _run_async(
        self,
        task: Task,
        router,
        worker_pool: Optional[List[str]],
        subagent_api_base: Optional[str],
        subagent_api_key: str,
        logs_dir: Optional[str],
    ) -> VerifyResult:
        from ..executors import DockerExecutor
        from agent_system.agents.subagent import SubAgent

        cfg = task.raw.get("config", {})
        task_dir = Path(task.raw.get("task_dir", ""))
        if not cfg or not task_dir:
            return VerifyResult(task.task_id, 0.0, error="missing config/task_dir")

        base = Path(logs_dir or "/tmp/tb_runs") / task.task_id
        verifier_logs = base / "verifier"
        agent_logs = base / "agent"
        verifier_logs.mkdir(parents=True, exist_ok=True)
        agent_logs.mkdir(parents=True, exist_ok=True)

        # Resolve SubAgent endpoint: prefer the worker API base set on the router
        if subagent_api_base is None:
            for attr in ("sub_model_api_base", "api_base", "_api_base"):
                if hasattr(router, attr):
                    subagent_api_base = getattr(router, attr)
                    if subagent_api_base:
                        break
        if subagent_api_base is None:
            from ..config import DEFAULT_API_BASE
            subagent_api_base = DEFAULT_API_BASE

        subagent = SubAgent(
            api_base=subagent_api_base,
            api_key=subagent_api_key,
            max_steps=self.subagent_max_steps,
            cmd_timeout=self.subagent_cmd_timeout,
        )

        executor = DockerExecutor(
            task_id=task.task_id,
            task_dir=task_dir,
            task_config=cfg,
            verifier_logs_dir=verifier_logs,
            agent_logs_dir=agent_logs,
            docker_manager=self._get_docker_manager(),
            docker_timeout=self.docker_timeout,
        )

        instruction = task.context.get("task_instruction", task.question)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"## Task\n{instruction}\n\n"
                f"## Planner budget\nYou have {self.max_attempts} delegation attempts.\n"
            )},
        ]

        trajectory: List[Dict[str, Any]] = []
        reward = 0.0
        submit_called = False
        last_error: Optional[str] = None

        try:
            await executor.start_container()

            for attempt in range(self.max_attempts):
                # Inject a short budget note (not persisted in trajectory ctx)
                live_messages = messages + [
                    {"role": "system", "content": _budget_note(attempt + 1, self.max_attempts)}
                ]

                try:
                    resp = router.chat_completions(live_messages, tools=TOOLS)
                except NotImplementedError as e:
                    last_error = f"router {type(router).__name__} lacks chat_completions: {e}"
                    break
                except Exception as e:
                    last_error = f"planner call failed: {e}"
                    break

                content = resp.get("content") or ""
                tool_calls = resp.get("tool_calls") or []
                assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": t["id"], "type": "function",
                         "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}}
                        for t in tool_calls
                    ]
                messages.append(assistant_msg)

                trajectory.append({
                    "attempt": attempt + 1,
                    "planner_content": content,
                    "tool_calls": tool_calls,
                })

                if not tool_calls:
                    # No structured action — treat as planner refusal and stop.
                    last_error = "planner returned no tool call"
                    break

                # Process each tool call (usually one per turn)
                did_submit = False
                for tc in tool_calls:
                    name = tc.get("name")
                    args = tc.get("arguments", {}) or {}
                    tc_id = tc.get("id", f"tc_{attempt}")

                    if name == "submit":
                        reason = args.get("reason", "(no reason)")
                        trajectory[-1]["submit"] = {"reason": reason}
                        messages.append({
                            "role": "tool", "tool_call_id": tc_id,
                            "content": "Submission received; verifier will run.",
                        })
                        did_submit = True
                        break  # stop processing further tool calls this turn

                    if name == "delegate_task":
                        worker_model = args.get("worker_model") or ""
                        subtask_instruction = args.get("instruction") or ""
                        # Clamp worker to the allowed pool for baselines
                        if worker_pool and worker_model not in worker_pool:
                            worker_model = worker_pool[0]
                        if not subtask_instruction.strip():
                            msg = "Empty instruction; delegate_task skipped."
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": msg})
                            trajectory[-1]["delegate"] = {"error": msg}
                            continue

                        try:
                            sub_result = await subagent.run(
                                model=worker_model,
                                task_instruction=subtask_instruction,
                                original_question=instruction,
                                executor=executor,
                                agent_logs_dir=agent_logs,
                            )
                        except Exception as e:
                            sub_result = {
                                "status": "error",
                                "completed": [], "issues": [str(e)[:300]],
                                "message": f"SubAgent crashed: {e}",
                                "steps_taken": 0, "model": worker_model, "commands_log": [],
                            }

                        planner_view = SubAgent.format_result_for_planner(sub_result)
                        messages.append({
                            "role": "tool", "tool_call_id": tc_id,
                            "content": planner_view,
                        })
                        trajectory[-1]["delegate"] = {
                            "worker_model": worker_model,
                            "instruction": subtask_instruction[:500],
                            "sub_result": {
                                k: sub_result.get(k) for k in (
                                    "status", "steps_taken", "completed", "issues", "message",
                                )
                            },
                        }
                    else:
                        msg = f"Unknown tool '{name}'; ignored."
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": msg})

                if did_submit:
                    submit_called = True
                    break

            # Run tests once — either because planner submitted or budget ran out.
            try:
                reward = float(await executor.run_tests() or 0.0)
            except Exception as e:
                last_error = f"run_tests failed: {e}"
                reward = 0.0

        except Exception as e:
            last_error = f"pipeline exception: {e}"
            logger.exception("[%s] interactive pipeline failed", task.task_id)
        finally:
            try:
                await executor.cleanup()
            except Exception as e:
                logger.warning("[%s] cleanup failed: %s", task.task_id, e)

        # Save trajectory
        try:
            with (base / "trajectory.json").open("w") as f:
                json.dump({
                    "task_id": task.task_id,
                    "reward": reward,
                    "submit_called": submit_called,
                    "attempts_used": len(trajectory),
                    "max_attempts": self.max_attempts,
                    "last_error": last_error,
                    "trajectory": trajectory,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        return VerifyResult(
            task.task_id, reward,
            error=last_error,
            log=json.dumps({"attempts": len(trajectory), "submit": submit_called})[:3000],
        )
