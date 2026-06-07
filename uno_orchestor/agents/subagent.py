"""SubAgent: multi-turn Docker executor for TerminalBench pipeline.

Designed to be called from Planner's execute_subtask callback.
Each invocation runs a multi-turn loop:
  1. SubAgent LLM receives task instruction + Docker observation
  2. Outputs structured JSON: {"action":"execute","params":{"command":"..."}}
  3. Command runs in Docker, output returned as next observation
  4. Repeat until SubAgent calls "finish" or hits max_steps

Returns a structured report string that the Planner can interpret.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ── SubAgent system prompt ──────────────────────────────────────

SUBAGENT_SYSTEM_PROMPT = """\
You are an autonomous shell executor running INSIDE a Docker container as root.
You DIRECTLY execute commands — you are NOT a chatbot giving advice.
NEVER say "I can't run commands" — you ARE the one running them.

Each turn you receive the output of your previous command. Respond with the next command.

RULES:
- ONE command per turn. Wait for output before the next step.
- For package installs: use DEBIAN_FRONTEND=noninteractive and -y flags.
- If dpkg lock error: run `kill $(lsof -t /var/lib/dpkg/lock-frontend) 2>/dev/null; rm -f /var/lib/dpkg/lock*` first.
- Prefer pip over apt when possible (faster, fewer lock issues).
- Long commands: chain with && to avoid partial failure.
- If a command times out or fails, try a simpler alternative.
- You MUST call finish before running out of steps!

OUTPUT FORMAT:
⚠️ CRITICAL: Reply with ONLY a raw JSON object. No markdown, no explanation, no prose.

To execute a command:
{"action": "execute", "params": {"command": "your shell command"}, "memory": "key findings"}

When done (or before running out of steps):
{"action": "finish", "params": {"status": "done", "completed": ["step1", "step2"], "issues": [], "message": "summary"}, "memory": "final notes"}
"""


class SubAgent:
    """Multi-turn sub-agent that executes commands in a Docker container.

    Args:
        api_base: OpenAI-compatible API endpoint for the sub-agent model.
        api_key: API key.
        max_steps: Maximum number of interaction turns.
        cmd_timeout: Timeout in seconds for each Docker command.
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        max_steps: int = 30,
        cmd_timeout: int = 300,
    ):
        self.client = AsyncOpenAI(
            base_url=api_base, api_key=api_key, timeout=90,
        )
        self.max_steps = max_steps
        self.cmd_timeout = cmd_timeout

    async def run(
        self,
        model: str,
        task_instruction: str,
        original_question: str,
        executor: Any,
        agent_logs_dir: Any = None,
    ) -> dict:
        """Run multi-turn SubAgent loop in Docker.

        Args:
            model: API model ID to use.
            task_instruction: Specific instruction from Planner for this subtask.
            original_question: The original TerminalBench task for reference.
            executor: DockerExecutor instance (must have execute_command method).
            agent_logs_dir: Optional path for command logs.

        Returns:
            dict with keys: status, completed, issues, message, steps_taken, model
        """
        messages = [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"[Step 1/{self.max_steps}]\n\n"
                f"== Your Task ==\n{task_instruction}\n\n"
                f"== Original Task (for reference) ==\n{original_question}\n\n"
                "Environment ready. Begin executing commands."
            )},
        ]

        finish_result = None
        steps_taken = 0
        commands_log = []

        for step in range(self.max_steps):
            steps_taken = step + 1
            remaining = self.max_steps - steps_taken

            # Budget warning
            if remaining <= 3 and step > 0:
                messages.append({"role": "user", "content": (
                    f"WARNING: Only {remaining} steps left! "
                    "Use 'finish' NOW to report your progress!"
                )})

            # Call SubAgent LLM (with 1 retry on transient errors)
            raw = ""
            llm_error = None
            for _attempt in range(2):
                try:
                    resp = await self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.1,
                        max_tokens=2048,
                        extra_body={"enable_thinking": False},
                    )
                    raw = resp.choices[0].message.content or ""
                    llm_error = None
                    break
                except Exception as e:
                    llm_error = str(e)
                    logger.warning("[SubAgent step %d] LLM error (attempt %d): %s",
                                   steps_taken, _attempt + 1, llm_error[:200])
                    if "quota" in llm_error.lower() or "balance" in llm_error.lower():
                        break  # No point retrying quota errors
                    import asyncio as _aio
                    await _aio.sleep(2)

            if llm_error:
                # Surface error to Planner instead of silent break
                finish_result = {
                    "status": "error",
                    "completed": [],
                    "issues": [f"API error: {llm_error[:300]}"],
                    "message": f"SubAgent API failed at step {steps_taken}: {llm_error[:200]}",
                }
                break

            messages.append({"role": "assistant", "content": raw})

            # Parse action. Wrap parsing+dispatch in try/except so a single
            # malformed response never bubbles out of the whole SubAgent loop.
            try:
                action = _parse_action(raw)
                action_type = action.get("action", "") if isinstance(action, dict) else ""
                params = action.get("params", {}) if isinstance(action, dict) else {}
                # Defensive: some LLMs emit params as a plain string or null
                # instead of a dict. Coerce so later `.get(...)` calls don't
                # raise 'str has no attribute get'.
                if isinstance(params, str):
                    if action_type == "execute":
                        params = {"command": params}
                    elif action_type == "finish":
                        params = {"message": params}
                    else:
                        params = {}
                elif not isinstance(params, dict):
                    params = {}
            except Exception as _parse_err:
                logger.warning("[SubAgent step %d] parse failure: %s",
                               steps_taken, _parse_err)
                messages.append({"role": "user", "content": json.dumps({
                    "error": f"Could not parse your response: {str(_parse_err)[:120]}",
                    "hint": 'Reply with ONLY a JSON object like '
                            '{"action":"execute","params":{"command":"..."}}'
                })})
                continue

            # ── finish ──
            if action_type == "finish":
                finish_result = {
                    "status": params.get("status", "done"),
                    "completed": params.get("completed", []),
                    "issues": params.get("issues", []),
                    "message": params.get("message", ""),
                }
                break

            # ── execute ──
            if action_type == "execute":
                command = params.get("command", "")
                if not command:
                    messages.append({"role": "user", "content": json.dumps({
                        "error": "No command provided.",
                        "hint": 'Use {"action":"execute","params":{"command":"..."}}'
                    })})
                    continue

                output, exit_code = await self._exec(executor, command)
                commands_log.append((steps_taken, command, exit_code, output[:500]))

                # Log to file
                if agent_logs_dir:
                    self._write_cmd_log(
                        agent_logs_dir, steps_taken, command, exit_code, output,
                    )

                # Feed observation back
                obs = {
                    "step": steps_taken,
                    "max_steps": self.max_steps,
                    "command": command,
                    "exit_code": exit_code,
                    "output": output[-2000:],
                }
                messages.append({"role": "user", "content": json.dumps(obs)})
                continue

            # ── unknown / malformed output ──
            fallback_cmd = _extract_single_command(raw)
            if fallback_cmd:
                output, exit_code = await self._exec(executor, fallback_cmd)
                commands_log.append((steps_taken, fallback_cmd, exit_code, output[:500]))
                if agent_logs_dir:
                    self._write_cmd_log(
                        agent_logs_dir, steps_taken, fallback_cmd, exit_code, output,
                    )
                obs = {
                    "step": steps_taken,
                    "max_steps": self.max_steps,
                    "exit_code": exit_code,
                    "output": output[-2000:],
                    "note": "Your output was not valid JSON. Reply with ONLY JSON.",
                }
                messages.append({"role": "user", "content": json.dumps(obs)})
            else:
                messages.append({"role": "user", "content": json.dumps({
                    "error": "Invalid response format. Reply with ONLY a JSON object.",
                    "hint": '{"action":"execute","params":{"command":"..."}} or '
                            '{"action":"finish","params":{"status":"done",...}}'
                })})

        if not finish_result:
            finish_result = {
                "status": "partial",
                "completed": [c[1] for c in commands_log[-3:]] if commands_log else [],
                "issues": [f"SubAgent exhausted {steps_taken}/{self.max_steps} steps without finish"],
                "message": f"Ran {steps_taken} steps, {len(commands_log)} commands executed",
            }

        return {
            **finish_result,
            "steps_taken": steps_taken,
            "model": model,
            "commands_log": commands_log,
        }

    async def _exec(self, executor, command: str) -> tuple[str, int]:
        """Execute a command in Docker with timeout handling."""
        try:
            output, exit_code = await executor.execute_command(
                command, timeout=self.cmd_timeout,
            )
            return output[-3000:], exit_code
        except Exception as e:
            return f"Command execution error: {e}", -1

    @staticmethod
    def _write_cmd_log(logs_dir, step, command, exit_code, output):
        try:
            from pathlib import Path
            log_file = Path(logs_dir) / "commands.log"
            with log_file.open("a") as f:
                f.write(
                    f"[Step {step}] {command}\n"
                    f"Exit: {exit_code}\n"
                    f"{output[:2000]}\n{'-' * 60}\n"
                )
        except Exception:
            pass

    @staticmethod
    def format_result_for_planner(result: dict) -> str:
        """Format SubAgent result as a string for the Planner's ToolMessage.

        The Planner sees this as the tool response from plan_subtask().
        """
        status = result.get("status", "?")
        model = result.get("model", "?")
        steps = result.get("steps_taken", 0)
        completed = result.get("completed", [])
        issues = result.get("issues", [])
        message = result.get("message", "")

        parts = [
            f"[SubAgent: {model}, {steps} steps, status={status}]",
        ]
        if completed:
            parts.append(f"Completed: {completed}")
        if issues:
            parts.append(f"Issues: {issues}")
        if message:
            parts.append(f"Message: {message}")

        # Include last few command outputs for context
        commands_log = result.get("commands_log", [])
        if commands_log:
            parts.append("Recent commands:")
            for step_n, cmd, exit_code, output in commands_log[-5:]:
                parts.append(f"  [{step_n}] $ {cmd}")
                parts.append(f"      exit={exit_code}")
                if output.strip():
                    # Truncate long outputs
                    out_lines = output.strip()[:300]
                    parts.append(f"      {out_lines}")

        return "\n".join(parts)


# ── Parsing helpers ─────────────────────────────────────────────

def _parse_action(raw: str) -> dict:
    """Parse SubAgent JSON output with fallbacks."""
    raw = raw.strip()
    # Strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
        raw = raw.strip()

    # Direct JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "action" in obj:
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Find JSON in text
    m = re.search(
        r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*(?:\{[^{}]*\}[^{}]*)?\}',
        raw, re.DOTALL,
    )
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "action" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # DISCUSSION/COMMAND format fallback
    cmd_match = re.search(r'COMMAND\s*\n(.+?)(?:\n\n|\Z)', raw, re.DOTALL)
    if cmd_match:
        cmd = cmd_match.group(1).strip().split("\n")[0].strip()
        if cmd.lower() == "finish":
            return {"action": "finish", "params": {"status": "done", "message": raw[:200]}}
        return {"action": "execute", "params": {"command": cmd}}

    return {"action": "unknown", "raw": raw[:500]}


def _extract_single_command(raw: str) -> str:
    """Try to extract a single executable command from free-form text."""
    # ```bash blocks
    m = re.search(r'```(?:bash|sh|shell)?\s*\n(.+?)```', raw, re.DOTALL)
    if m:
        lines = [
            l.strip() for l in m.group(1).strip().split("\n")
            if l.strip() and not l.strip().startswith("#")
        ]
        if lines:
            return " && ".join(lines)

    # Lines that look like commands
    for line in raw.split("\n"):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("{") and any(
            s.startswith(c) for c in [
                "sudo", "apt", "pip", "make", "gcc", "cd ", "mkdir",
                "wget", "curl", "git ", "chmod", "cp ", "mv ", "echo ",
                "export", "python", "npm", "cargo", "cmake", "tar ",
                "cat ", "tee ", "source", "dnf", "yum", "./", "bash ",
            ]
        ):
            return s
    return ""
