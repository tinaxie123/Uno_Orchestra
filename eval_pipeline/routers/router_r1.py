"""
Router-R1 adapter: <think> → <search>Model:Query</search> → <information> → <answer>
"""
import re
import openai
from .base import BaseRouter, RouteResult
from ..config import COST_PER_M, EVAL_MAX_TOKENS, SUB_AGENT_TEMP, DEFAULT_LOCAL_BASE, DEFAULT_API_BASE

ROUTER_PROMPT = """\
Answer the given question. \
Every time you receive new information, reason inside <think> ... </think>. \
Then call a specialized LLM via <search> LLM-Name:Your-Query </search>. \

STRICT FORMAT: Replace LLM-Name with EXACT name from [Claude-Haiku-4.5, Gemini-2.5-Flash, Kimi-K2.5, Claude-Sonnet-4.6, Gemini-3.1-Pro, GPT-5.3-Codex, Qwen3.6-Plus, Claude-Opus-4.6, GPT-5.4]. \
NEVER use literal "LLM-Name". Before each call, reason in <think> about which model and why. \
Response appears in <information>...</information>. When done: <answer>...</answer>. \

Models: \
Claude-Haiku-4.5($1.25) Gemini-2.5-Flash($1.50) Kimi-K2.5($2) Claude-Sonnet-4.6($15) \
Gemini-3.1-Pro($10) GPT-5.3-Codex($20) Qwen3.6-Plus($8) Claude-Opus-4.6($75) GPT-5.4($60) \
Question: {question}
"""

# Display name → API model ID
_NAME_MAP = {
    "claude-haiku-4.5": "claude-haiku-4-5-20251001",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "kimi-k2.5": "kimi-k2.5",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "qwen3.6-plus": "qwen3.6-plus",
    "claude-opus-4.6": "claude-opus-4-6",
    "gpt-5.4": "gpt-5.4",
}

_FUZZY = [
    ("codex", "gpt-5.3-codex"), ("5.4", "gpt-5.4"), ("opus", "claude-opus-4-6"),
    ("sonnet", "claude-sonnet-4-6"), ("haiku", "claude-haiku-4-5-20251001"),
    ("pro", "gemini-3.1-pro-preview"), ("flash", "gemini-2.5-flash"),
    ("qwen", "qwen3.6-plus"), ("kimi", "kimi-k2.5"),
    ("gpt", "gpt-5.4"), ("claude", "claude-sonnet-4-6"), ("gemini", "gemini-2.5-flash"),
]

DEFAULT_MODEL = "gpt-5.3-codex"


def _resolve(raw: str) -> str:
    t = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if not t or "llm-name" in t:
        return DEFAULT_MODEL
    for k, mid in _NAME_MAP.items():
        if k in t:
            return mid
    for kw, mid in _FUZZY:
        if kw in t:
            return mid
    return DEFAULT_MODEL


class RouterR1(BaseRouter):
    """Router-R1 (Qwen2.5-3B-Instruct), pre-trained model-selection router."""

    def __init__(self, local_base=DEFAULT_LOCAL_BASE, api_base=DEFAULT_API_BASE,
                 api_key="EMPTY", model_name="Router-R1-Qwen2.5-3B-Instruct",
                 max_turns=3, agent_prompt=""):
        self.local = openai.OpenAI(base_url=local_base, api_key="EMPTY")
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)
        self.model_name = model_name
        self.max_turns = max_turns
        self.agent_prompt = agent_prompt  # benchmark-specific sub-agent prompt

    @property
    def name(self):
        return "Router-R1"

    def route(self, question: str, context: dict = None) -> RouteResult:
        ctx = context or {}
        prompt = ROUTER_PROMPT.format(question=question)
        msgs = [{"role": "user", "content": prompt}]
        output = ""
        routes, models, cost, toks = 0, [], 0.0, 0

        for _ in range(self.max_turns + 1):
            try:
                r = self.local.chat.completions.create(
                    model=self.model_name, messages=msgs,
                    temperature=0.0, max_tokens=2048,
                    stop=["</search>", "</answer>"],
                )
            except Exception as e:
                output += f"\n[ERROR: {e}]"
                break

            o = r.choices[0].message.content or ""
            if "<answer>" in o:
                output += o + "</answer>"
                break
            if "<search>" in o:
                o += "</search>"
                m = re.search(r"<search>(.*?)(?:</search>|$)", o, re.DOTALL)
                if m:
                    raw = m.group(1).strip()
                    parts = raw.split(":", 1)
                    mid = _resolve(parts[0]) if len(parts) > 1 else DEFAULT_MODEL
                    query = parts[1].strip() if len(parts) > 1 else raw

                    # Build sub-agent prompt
                    sub_prompt = self.agent_prompt.format(query=query, **ctx) if self.agent_prompt else query
                    try:
                        sr = self.api.chat.completions.create(
                            model=mid,
                            messages=[{"role": "user", "content": sub_prompt}],
                            temperature=SUB_AGENT_TEMP, max_tokens=EVAL_MAX_TOKENS,
                        )
                        txt = sr.choices[0].message.content or ""
                        t = getattr(sr.usage, "completion_tokens", 0) or 0
                    except Exception as e:
                        txt, t = f"API Error: {e}", 0

                    routes += 1
                    models.append(mid)
                    toks += t
                    cost += COST_PER_M.get(mid, 10.0) * max(t, 1) / 1e6
                    output += o + f"\n<information>{txt}</information>\n"
                    msgs = [{"role": "user", "content": prompt + output}]
                else:
                    output += o; break
            elif o.strip():
                output += o; break
            else:
                break

        ans_m = re.search(r"<answer>(.*?)</answer>", output, re.DOTALL)
        return RouteResult(
            answer=ans_m.group(1).strip() if ans_m else output,
            full_trace=output, route_count=routes,
            routed_models=models, total_cost=cost, total_tokens=toks,
        )
