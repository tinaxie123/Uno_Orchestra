"""
Uno SFT adapter: <plan> → <route> → <obs> → <verify> → <final_answer>
Uses schema v1.1 with real API sub-agent calls (same as RL training env).
"""
import re
import openai
from .base import BaseRouter, RouteResult
from ..config import (COST_PER_M, EVAL_MAX_TOKENS, SUB_AGENT_TEMP,
                      DEFAULT_LOCAL_BASE, DEFAULT_API_BASE, SKILLS, resolve_model)

# Skill → system prompt (same as envs.py SKILL_PROMPTS)
SKILL_PROMPTS = {
    "direct_answer": "Answer the following question directly and concisely.",
    "reason": "Reason step by step about the following question, then give a final answer.",
    "web_search": "You are a search engine. Return the most relevant factual information for the query.",
    "symbolic_math": "You are a math solver. Compute the answer. Give only the numerical result.",
    "execute_python": "You are a Python executor. Write and execute Python code to solve the following. Return the output.",
    "execute_shell": "You are a shell executor. Run the command and return the output.",
    "database_query": "You are a database. Return the queried information.",
    "read_document": "You are a document reader. Extract the relevant information.",
    "read_code": "You are a code analyst. Analyze the code and answer the question.",
    "extract_field": "Extract the specific field or value requested.",
    "parse_structured": "Parse the structured data and return the requested information.",
    "fact_check": "Verify the following claim and state whether it is true or false with evidence.",
    "call_api": "You are an API endpoint. Return the requested data.",
}

# Regex parsers for schema v1.1
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)
VERIFY_RE = re.compile(r'<verify round="(\d+)" status="([^"]+)"', re.DOTALL)

SYSTEM_PROMPT_PATH = "/home/xieht/data/sft/system_prompt.txt"


class UnoSFT(BaseRouter):
    """Uno SFT checkpoint — learned routing with decomposition, no RL."""

    def __init__(self, local_base=DEFAULT_LOCAL_BASE, api_base=DEFAULT_API_BASE,
                 api_key="EMPTY", model_name="Uno-SFT",
                 max_rounds=3, system_prompt=None):
        self.local = openai.OpenAI(base_url=local_base, api_key="EMPTY")
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)
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

    def _call_sub_agent(self, model: str, skill: str, query: str, question: str):
        """Call real API as sub-agent, same as envs.py."""
        sys_prompt = SKILL_PROMPTS.get(skill, "Answer the following question concisely.")
        actual_model = resolve_model(model)
        try:
            resp = self.api.chat.completions.create(
                model=actual_model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"Original question: {question}\n\nSub-task: {query}\n\nAnswer directly."},
                ],
                max_tokens=EVAL_MAX_TOKENS,
                temperature=SUB_AGENT_TEMP,
            )
            text = resp.choices[0].message.content or ""
            tokens = getattr(resp.usage, "completion_tokens", 0) or 0
            cost = COST_PER_M.get(model, 10.0) * max(tokens, 1) / 1e6
            return text, tokens, cost
        except Exception as e:
            return f"API error: {str(e)[:200]}", 0, 0.0

    def route(self, question: str, context: dict = None) -> RouteResult:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        full_trace = ""
        all_models, all_skills = [], []
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
                text, tokens, cost = self._call_sub_agent(model, skill, query, question)
                all_models.append(model)
                all_skills.append(skill)
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
            total_cost=total_cost,
            total_tokens=total_tokens,
        )
