"""
Terminal-Bench 2.0 — Planner + SubAgent pipeline.

Two levels of agents, both our own code:

  Planner (``router.chat_completions``)
    → decides ``delegate_task(worker_model, instruction)`` or ``submit(reason)``

  SubAgent (``uno_orchestor.agents.subagent.SubAgent``)
    → runs multi-turn shell commands inside the Docker container,
      observes output, reports a structured status back to the Planner

The planner's view is a chat-completions call with two OpenAI tools:
``delegate_task`` and ``submit``. Routers that participate inherit the default
``BaseRouter.chat_completions`` (Direct, Oracle, Random) or override it with
their own orchestration (PlannerRouter / UnoSFT). When the planner
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
HARBOR_TASKS_DIR = os.environ.get(
    "TERMINAL_BENCH_TASKS_DIR",
    str(_REPO_ROOT / "data" / "terminal-bench" / "tasks"),
)
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


FLAT_SYSTEM_PROMPT = """\
You are completing a terminal / systems task inside a persistent Docker
container. You execute shell commands yourself, one per turn, observing each
output before the next command.

## Tools
- execute_command(command)
    Run a single shell command in the container. You will see stdout/stderr
    and the exit code. State persists between commands.
- submit(reason)
    Declare the whole task complete. The harness runs the task's test.sh;
    the reward file decides pass/fail.

## Rules
- One command per turn. Wait for the output, then choose the next command.
- You are root in the container. Ubuntu + apt + pip available.
- For apt: use DEBIAN_FRONTEND=noninteractive and -y flags; if a dpkg lock is
  held, kill and clean it before retrying.
- Chain with && when a sequence must succeed together. Long-running commands
  may time out.
- Call submit only after the task is actually complete.
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


FLAT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "Run a single shell command in the persistent Docker container "
                "and return its stdout/stderr and exit code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run (one per call).",
                    }
                },
                "required": ["command"],
            },
        },
    },
    TOOLS[1],  # submit tool — same as hierarchical mode
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
    scoring_mode = "uno_harness"
    score_name = "Uno harness score"

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

    def interactive_verify(self, task: Task, router, logs_dir=None) -> VerifyResult:
        """Adapter used by eval_pipeline.run's common interactive path."""
        from ..config import DEFAULT_API_BASE, MODEL_POOL

        flat_mode = router.name.startswith("Direct(")
        return self.run_interactive(
            task=task,
            router=router,
            worker_pool=MODEL_POOL,
            subagent_api_base=getattr(router, "sub_model_api_base", DEFAULT_API_BASE),
            subagent_api_key=getattr(router, "sub_model_api_key", "EMPTY"),
            logs_dir=logs_dir,
            flat_mode=flat_mode,
        )

    # ----- main interactive pipeline -----------------------------------

    # ------------------------------------------------------------------
    # Hierarchical two-step pipeline: Planner → Router → Worker, matching
    # the SFT training schema (plan_subtask + finish / route(model, skill)).
    # ------------------------------------------------------------------

    def run_hierarchical(
        self,
        task: Task,
        planner_model: str,
        planner_api_base: str,
        planner_api_key: str,
        router_model: str,
        router_api_base: str,
        router_api_key: str,
        pools,
        sub_model_api_base: str,
        sub_model_api_key: str,
        logs_dir: Optional[str] = None,
        random_worker: bool = False,
    ) -> VerifyResult:
        """Two-step schema: Planner loops with [plan_subtask, finish]; each
        plan_subtask invokes the Router (which picks (model, skill)) and then a
        worker. Shell/Python skills run in Docker via SubAgent; other skills
        are single-shot API calls. When the Planner calls finish(answer) we
        run ``test.sh`` to decide the reward.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._run_hierarchical_async(
                    task=task,
                    planner_model=planner_model,
                    planner_api_base=planner_api_base,
                    planner_api_key=planner_api_key,
                    router_model=router_model,
                    router_api_base=router_api_base,
                    router_api_key=router_api_key,
                    pools=pools,
                    sub_model_api_base=sub_model_api_base,
                    sub_model_api_key=sub_model_api_key,
                    logs_dir=logs_dir,
                    random_worker=random_worker,
                )
            )
        finally:
            loop.close()

    async def _run_hierarchical_async(
        self,
        task: Task,
        planner_model: str,
        planner_api_base: str,
        planner_api_key: str,
        router_model: str,
        router_api_base: str,
        router_api_key: str,
        pools,
        sub_model_api_base: str,
        sub_model_api_key: str,
        logs_dir: Optional[str],
        random_worker: bool,
    ) -> VerifyResult:
        import random as _random
        from openai import AsyncOpenAI
        return VerifyResult(
            task.task_id,
            0.0,
            error=(
                "legacy run_hierarchical depended on deleted planner/router modules; "
                "use interactive_verify/run_interactive for reproducible evaluation"
            ),
        )
        from ..executors import DockerExecutor
        from uno_orchestor.agents.subagent import SubAgent

        cfg = task.raw.get("config", {})
        task_dir = Path(task.raw.get("task_dir", ""))
        if not cfg or not task_dir:
            return VerifyResult(task.task_id, 0.0, error="missing config/task_dir")

        base = Path(logs_dir or "/tmp/tb_runs_hier") / task.task_id
        verifier_logs = base / "verifier"
        agent_logs = base / "agent"
        verifier_logs.mkdir(parents=True, exist_ok=True)
        agent_logs.mkdir(parents=True, exist_ok=True)

        executor = DockerExecutor(
            task_id=task.task_id,
            task_dir=task_dir,
            task_config=cfg,
            verifier_logs_dir=verifier_logs,
            agent_logs_dir=agent_logs,
            docker_manager=self._get_docker_manager(),
            docker_timeout=self.docker_timeout,
        )

        sub_client = AsyncOpenAI(base_url=sub_model_api_base, api_key=sub_model_api_key, timeout=90)
        subagent = SubAgent(
            api_base=sub_model_api_base,
            api_key=sub_model_api_key,
            max_steps=self.subagent_max_steps,
            cmd_timeout=self.subagent_cmd_timeout,
        )

        SHELL_SKILLS = {"execute_shell", "execute_python", "execute_bash"}
        routing_decisions: List[Dict[str, Any]] = []
        rng = _random.Random(0)

        async def execute_subtask(instruction: str, task_id: str) -> str:
            selected_model, selected_skill = await aroute_subtask(
                instruction=instruction,
                model=router_model, api_base=router_api_base, api_key=router_api_key,
                pools=pools, temperature=0.3,
            )
            if random_worker and pools.get("models"):
                selected_model = rng.choice(pools["models"])
                allowed = pools.get("model_skills", {}).get(selected_model) or pools.get("skills", [])
                if allowed:
                    selected_skill = rng.choice(allowed)
            routing_decisions.append({
                "task_id": task_id, "instruction": instruction[:400],
                "routed_model": selected_model, "routed_skill": selected_skill,
            })

            if selected_skill in SHELL_SKILLS:
                try:
                    result = await subagent.run(
                        model=selected_model,
                        task_instruction=instruction,
                        original_question=task.question,
                        executor=executor,
                        agent_logs_dir=agent_logs,
                    )
                except Exception as e:
                    return f"[routed to {selected_model} / {selected_skill}]\nSubAgent crashed: {e}"
                return SubAgent.format_result_for_planner({**result, "model": selected_model})
            # Non-shell skill: single API call
            try:
                resp = await sub_client.chat.completions.create(
                    model=selected_model,
                    messages=[{"role": "user", "content": instruction}],
                    temperature=0.1, max_tokens=1024,
                )
                txt = (resp.choices[0].message.content or "").strip()
                return f"[routed to {selected_model} / {selected_skill}]\n{txt}"
            except Exception as e:
                return f"[routed to {selected_model} / {selected_skill}]\nWorker error: {e}"

        reward = 0.0
        planner_answer: Optional[str] = None
        last_error: Optional[str] = None
        planner_result: Dict[str, Any] = {}
        try:
            await executor.start_container()
            planner_result = await arun_planner(
                question=task.question,
                model=planner_model,
                api_base=planner_api_base,
                api_key=planner_api_key,
                execute_subtask_fn=execute_subtask,
                temperature=0.7,
            )
            planner_answer = planner_result.get("answer")
            try:
                reward = float(await executor.run_tests() or 0.0)
            except Exception as e:
                last_error = f"run_tests failed: {e}"
                reward = 0.0
        except Exception as e:
            last_error = f"pipeline exception: {e}"
            logger.exception("[%s] hierarchical pipeline failed", task.task_id)
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
                    "mode": "hierarchical",
                    "planner_model": planner_model,
                    "router_model": router_model,
                    "random_worker": random_worker,
                    "reward": reward,
                    "planner_answer": planner_answer,
                    "last_error": last_error,
                    "subtasks": planner_result.get("subtasks", []),
                    "routing_decisions": routing_decisions,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        return VerifyResult(
            task.task_id, reward,
            error=last_error,
            log=json.dumps({
                "subtasks": len(routing_decisions),
                "answer": (planner_answer or "")[:300],
            })[:3000],
        )

    # ------------------------------------------------------------------
    # Legacy: single-tool-call delegate pipeline kept for reference.
    # ------------------------------------------------------------------

    def run_interactive(
        self,
        task: Task,
        router,
        worker_pool: Optional[List[str]] = None,
        subagent_api_base: Optional[str] = None,
        subagent_api_key: str = "EMPTY",
        logs_dir: Optional[str] = None,
        flat_mode: bool = False,
    ) -> VerifyResult:
        """Run the router on ``task``.

        Args:
            task: A Task from ``self.load()``.
            router: A BaseRouter with ``chat_completions(messages, tools)``.
            worker_pool: Hierarchical mode only — worker models the Planner may
                delegate to. Ignored in flat mode.
            subagent_api_base: Hierarchical mode — base URL for the SubAgent's
                worker LLM. Ignored in flat mode.
            subagent_api_key: SubAgent API key (hierarchical mode).
            logs_dir: Where to save trajectory + commands log.
            flat_mode: If True, run as a **direct** single-agent baseline:
                the router itself outputs ``execute_command``/``submit`` tool
                calls and interacts with the Docker container directly. No
                delegation layer. Useful for Direct(X) baselines.
        """
        loop = asyncio.new_event_loop()
        try:
            if flat_mode:
                return loop.run_until_complete(
                    self._run_flat_async(task, router, logs_dir)
                )
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
        from uno_orchestor.agents.subagent import SubAgent

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

    # ------------------------------------------------------------------
    # Flat / Direct-baseline pipeline: single model ↔ Docker, no Planner
    # ------------------------------------------------------------------

    async def _run_flat_async(
        self,
        task: Task,
        router,
        logs_dir: Optional[str],
    ) -> VerifyResult:
        from ..executors import DockerExecutor

        cfg = task.raw.get("config", {})
        task_dir = Path(task.raw.get("task_dir", ""))
        if not cfg or not task_dir:
            return VerifyResult(task.task_id, 0.0, error="missing config/task_dir")

        base = Path(logs_dir or "/tmp/tb_runs_flat") / task.task_id
        verifier_logs = base / "verifier"
        agent_logs = base / "agent"
        verifier_logs.mkdir(parents=True, exist_ok=True)
        agent_logs.mkdir(parents=True, exist_ok=True)

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
            {"role": "system", "content": FLAT_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"## Task\n{instruction}\n\n"
                f"## Budget\nYou may run up to {self.subagent_max_steps} commands. "
                f"Call submit only after verifying the task is complete.\n"
            )},
        ]

        trajectory: List[Dict[str, Any]] = []
        reward = 0.0
        submit_called = False
        last_error: Optional[str] = None

        try:
            await executor.start_container()

            for step in range(self.subagent_max_steps):
                # Budget note (ephemeral)
                live_messages = messages + [
                    {"role": "system",
                     "content": f"Budget: {self.subagent_max_steps - step} command(s) remaining."}
                ]

                try:
                    resp = router.chat_completions(live_messages, tools=FLAT_TOOLS)
                except NotImplementedError as e:
                    last_error = f"router {type(router).__name__} lacks chat_completions: {e}"
                    break
                except Exception as e:
                    last_error = f"router call failed: {e}"
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
                    "step": step + 1,
                    "content": content,
                    "tool_calls": tool_calls,
                })

                if not tool_calls:
                    last_error = "router returned no tool call"
                    break

                did_submit = False
                for tc in tool_calls:
                    name = tc.get("name")
                    args = tc.get("arguments", {}) or {}
                    tc_id = tc.get("id", f"tc_{step}")

                    if name == "submit":
                        trajectory[-1]["submit"] = {"reason": args.get("reason", "")}
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": "Submission received; verifier will run."})
                        did_submit = True
                        break

                    if name == "execute_command":
                        command = (args.get("command") or "").strip()
                        if not command:
                            messages.append({"role": "tool", "tool_call_id": tc_id,
                                             "content": "Empty command; ignored."})
                            continue
                        try:
                            output, exit_code = await executor.execute_command(
                                command, timeout=self.subagent_cmd_timeout,
                            )
                        except Exception as e:
                            output, exit_code = f"exec error: {e}", -1
                        output = output[-3000:]
                        obs = json.dumps({
                            "exit_code": exit_code,
                            "output": output[-2000:],
                        })
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": obs})
                        trajectory[-1]["execute"] = {
                            "command": command[:500],
                            "exit_code": exit_code,
                            "output_tail": output[-500:],
                        }
                    else:
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": f"Unknown tool '{name}'; ignored."})

                if did_submit:
                    submit_called = True
                    break

            try:
                reward = float(await executor.run_tests() or 0.0)
            except Exception as e:
                last_error = f"run_tests failed: {e}"
                reward = 0.0

        except Exception as e:
            last_error = f"pipeline exception: {e}"
            logger.exception("[%s] flat pipeline failed", task.task_id)
        finally:
            try:
                await executor.cleanup()
            except Exception as e:
                logger.warning("[%s] cleanup failed: %s", task.task_id, e)

        try:
            with (base / "trajectory.json").open("w") as f:
                json.dump({
                    "task_id": task.task_id,
                    "mode": "flat",
                    "reward": reward,
                    "submit_called": submit_called,
                    "steps_used": len(trajectory),
                    "max_steps": self.subagent_max_steps,
                    "last_error": last_error,
                    "trajectory": trajectory,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        return VerifyResult(
            task.task_id, reward,
            error=last_error,
            log=json.dumps({"steps": len(trajectory), "submit": submit_called})[:3000],
        )
