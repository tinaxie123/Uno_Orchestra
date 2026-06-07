"""
LocalRouter: local LLM selects which API model to call as sub-agent.

Flow: <think> → <search>Model:Query</search> → <information> → <answer>
"""
import re
import openai
from .base import BaseRouter, RouteResult
from ..config import EVAL_MAX_TOKENS, SUB_AGENT_TEMP, DEFAULT_LOCAL_BASE, DEFAULT_API_BASE, compute_cost

ROUTER_PROMPT = """\
Answer the given question. \
Every time you receive new information, reason inside <think> ... </think>. \
Then call a specialized LLM via <search> LLM-Name:Your-Query </search>. \

STRICT FORMAT: Replace LLM-Name with EXACT name from [Gemini-2.5-Flash-Lite, Gemini-2.5-Flash, Kimi-K2.5, Gemini-3-Flash-Preview, GPT-5.3-Codex, GPT-5.4, Claude-Sonnet-4.6, Claude-Opus-4.6]. \
NEVER use literal "LLM-Name". Before each call, reason in <think> about which model and why. \
Response appears in <information>...</information>. When done: <answer>...</answer>. \

Models (input/output $/1M tokens): \
Gemini-2.5-Flash-Lite($0.10/$0.40) Gemini-2.5-Flash($0.30/$2.50) Kimi-K2.5($0.60/$3) \
Gemini-3-Flash-Preview($0.50/$3) GPT-5.3-Codex($1.75/$14) \
GPT-5.4($2.50/$15) Claude-Sonnet-4.6($3/$15) Claude-Opus-4.6($5/$25) \
Question: {question}
"""

# Display name → API model ID
_NAME_MAP = {
    "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "kimi-k2.5": "kimi-k2.5",
    "gemini-3-flash-preview": "gemini-3-flash-preview",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.4": "gpt-5.4",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-opus-4.6": "claude-opus-4-6",
}

_FUZZY = [
    ("codex", "gpt-5.3-codex"), ("5.4", "gpt-5.4"), ("opus", "claude-opus-4-6"),
    ("sonnet", "claude-sonnet-4-6"),
    ("flash-lite", "gemini-2.5-flash-lite"), ("flash-preview", "gemini-3-flash-preview"),
    ("flash", "gemini-2.5-flash"), ("kimi", "kimi-k2.5"),
    ("gpt", "gpt-5.4"), ("claude", "claude-sonnet-4-6"), ("gemini", "gemini-2.5-flash"),
]

DEFAULT_MODEL = "gemini-3-flash-preview"


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


class LocalRouter(BaseRouter):
    """Local LLM as router: picks API model + forwards query as sub-agent call."""

    def __init__(self, local_base=DEFAULT_LOCAL_BASE, api_base=DEFAULT_API_BASE,
                 api_key="EMPTY", model_name="Qwen/Qwen2.5-7B-Instruct",
                 max_turns=3, agent_prompt=""):
        self.local = openai.OpenAI(base_url=local_base, api_key="EMPTY")
        self.api = openai.OpenAI(base_url=api_base, api_key=api_key)
        self.model_name = model_name
        self.max_turns = max_turns
        self.agent_prompt = agent_prompt
        self.system_prompt = ROUTER_PROMPT  # exposed for interactive mode

    @property
    def name(self):
        return f"LocalRouter({self.model_name.split('/')[-1]})"

    def route(self, question: str, context: dict = None) -> RouteResult:
        ctx = context or {}
        prompt = ROUTER_PROMPT.format(question=question)
        msgs = [{"role": "user", "content": prompt}]
        output = ""
        routes, models, cost, toks, prompt_toks = 0, [], 0.0, 0, 0

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

                    sub_prompt = self.agent_prompt.format(query=query, **ctx) if self.agent_prompt else query
                    try:
                        sr = self.api.chat.completions.create(
                            model=mid,
                            messages=[{"role": "user", "content": sub_prompt}],
                            temperature=SUB_AGENT_TEMP, max_tokens=EVAL_MAX_TOKENS,
                        )
                        txt = sr.choices[0].message.content or ""
                        t = getattr(sr.usage, "completion_tokens", 0) or 0
                        pt = getattr(sr.usage, "prompt_tokens", 0) or 0
                    except Exception as e:
                        txt, t, pt = f"API Error: {e}", 0, 0

                    routes += 1
                    models.append(mid)
                    toks += t
                    prompt_toks += pt
                    cost += compute_cost(mid, t, pt)
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
            routed_models=models, total_cost=cost, total_tokens=toks + prompt_toks,
            prompt_tokens=prompt_toks, completion_tokens=toks,
        )
