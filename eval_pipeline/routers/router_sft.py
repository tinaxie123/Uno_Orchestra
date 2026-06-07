"""
Uno SFT adapter: <plan> → <route> → <obs> → <verify> → <final_answer>
Uses schema v1.1 with real API sub-agent calls (same as RL training env).
"""
import os
import re
import openai
from .base import BaseRouter, RouteResult
from ..config import DEFAULT_LOCAL_BASE, DEFAULT_API_BASE, compute_cost, resolve_model
from uno_orchestor.routing.uno.harness import build_default_harness
from uno_orchestor.routing.uno.primitives import Route

# Regex parsers for schema v1.1
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)
VERIFY_RE = re.compile(r'<verify round="(\d+)" status="([^"]+)"', re.DOTALL)

SYSTEM_PROMPT_PATH = os.environ.get(
    "UNO_SYSTEM_PROMPT",
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../configs/uno/system_prompt.txt")
    ),
)


class UnoSFT(BaseRouter):
    """Uno SFT checkpoint — learned routing with decomposition, no RL."""

    def __init__(self, local_base=DEFAULT_LOCAL_BASE, api_base=DEFAULT_API_BASE,
                 api_key="EMPTY", model_name="Uno-SFT",
                 max_rounds=3, system_prompt=None):
        self.local = openai.OpenAI(base_url=local_base, api_key="EMPTY")
        self.harness = build_default_harness(
            api_key=api_key,
            base_url=api_base,
            model_resolver=resolve_model,
        )
        self.model_name = model_name
        self.max_rounds = max_rounds
        # Load system prompt
        if system_prompt:
            self.system_prompt = system_prompt
        else:
            try:
                with open(SYSTEM_PROMPT_PATH) as f:
                    self.system_prompt = f.read()
            except FileNotFoundError:
                self.system_prompt = "You are a routing agent."

    @property
    def name(self):
        return "Uno-SFT"

    def _call_sub_agent(
        self,
        round_n: int,
        subtask_id: int,
        model: str,
        skill: str,
        query: str,
        question: str,
    ):
        """Dispatch through the same primitive pool used by the RL env."""
        try:
            result = self.harness.run_route(
                Route(round=round_n, subtask=subtask_id, model=model, skill=skill, query=query),
                question,
            )
            cost = compute_cost(model, result.output_tokens) if result.billable else 0.0
            return result.text, result.output_tokens, cost, result.backend
        except Exception as e:
            return f"API error: {str(e)[:200]}", 0, 0.0, "harness_error"

    def route(self, question: str, context: dict = None) -> RouteResult:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        full_trace = ""
        all_models, all_skills, all_backends = [], [], []
        total_cost, total_tokens, route_count = 0.0, 0, 0

        for round_idx in range(self.max_rounds):
            # Get assistant response
            try:
                resp = self.local.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=2048,
                )
                assistant_text = resp.choices[0].message.content or ""
            except Exception as e:
                full_trace += f"\n[ERROR: {e}]"
                break

            full_trace += f"\n[ASSISTANT]\n{assistant_text}\n[/ASSISTANT]\n"
            messages.append({"role": "assistant", "content": assistant_text})

            # Check for final_answer (lazy mode or after verify pass)
            final_match = FINAL_RE.search(assistant_text)
            if final_match:
                break

            # Parse routes and call sub-agents
            routes = ROUTE_RE.findall(assistant_text)
            if not routes:
                break  # No routes and no final_answer = malformed

            obs_parts = []
            for round_n, subtask_id, model, skill, query in routes:
                route_count += 1
                text, tokens, cost, backend = self._call_sub_agent(
                    int(round_n),
                    int(subtask_id),
                    model,
                    skill,
                    query,
                    question,
                )
                all_models.append(model)
                all_skills.append(skill)
                all_backends.append(backend)
                total_tokens += tokens
                total_cost += cost
                obs_parts.append(f'<obs subtask="{subtask_id}">{text}</obs>')

            # Add tool response (observations)
            tool_content = "\n".join(obs_parts)
            full_trace += f"[TOOL]\n{tool_content}\n[/TOOL]\n"
            messages.append({"role": "user", "content": tool_content})

            # The model should now generate <verify> + possibly <final_answer> or <plan round=N+1>
            # Loop continues to get the next assistant turn

        # Extract final answer
        final_match = FINAL_RE.search(full_trace)
        answer = final_match.group(1).strip() if final_match else full_trace

        return RouteResult(
            answer=answer,
            full_trace=full_trace,
            route_count=route_count,
            routed_models=all_models,
            routed_skills=all_skills,
            routed_backends=all_backends,
            total_cost=total_cost,
            total_tokens=total_tokens,
            prompt_tokens=0,
            completion_tokens=total_tokens,
        )
