from __future__ import annotations
import argparse
import json
import os
import random
import re
import sys
from collections import defaultdict
import yaml
from tqdm import tqdm

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.insert(0, REPO_ROOT)
from configs import PoolConfig, load_pools
RECIPE_PATH = os.path.join(REPO_ROOT, "configs/sft/data/sft_recipe.yaml")
POOLS_PATH = os.path.join(REPO_ROOT, "configs/pools.yaml")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "data/sft")

MAX_STEPS = 8
MAX_TRAINING_TOKENS = 8192
ROUTER_PASS_K = 3

def load_recipe(path: str) -> list[dict]:
    with open(path) as f:
        return yaml.safe_load(f)["datasets"]

def build_system_prompt(pools: PoolConfig) -> str:
    model_list = ", ".join(pools["models"])
    skill_list = ", ".join(pools["skills"])
    skill_lines = []
    for mid, skills in sorted(pools["model_skills"].items()):
        skill_lines.append(f"  {mid}: [{', '.join(skills)}]")

    return f"""You are a task orchestrator that solves problems by delegating sub-tasks to specialized models.

Available models: {model_list}
Available skills: {skill_list}

Per-model allowed skills:
{chr(10).join(skill_lines)}

You have two tools:
- delegate_task(instruction, model, skill): delegate a sub-task to a model with a specific skill
- finish(answer): provide your final answer

Think step by step. Pick the cheapest model that can handle each sub-task. When you have enough information, call finish."""


def build_tools(pools: PoolConfig) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "delegate_task",
            "description": "Delegate a sub-task to a specialized model",
            "parameters": {"type": "object", "properties": {
                "instruction": {"type": "string", "description": "What the model should do"},
                "model": {"type": "string", "enum": pools["models"]},
                "skill": {"type": "string", "enum": pools["skills"]},
            }, "required": ["instruction", "model", "skill"]},
        }},
        {"type": "function", "function": {
            "name": "finish",
            "description": "Provide the final answer",
            "parameters": {"type": "object", "properties": {
                "answer": {"type": "string"},
            }, "required": ["answer"]},
        }},
    ]

def run_agent(
    question: str,
    model: str,
    api_base: str,
    api_key: str,
    sub_model_api_base: str,
    sub_model_api_key: str,
    pools: PoolConfig,
    temperature: float = 0.3,
) -> dict:
    from openai import OpenAI

    client = OpenAI(base_url=api_base, api_key=api_key, timeout=120)
    sub_client = OpenAI(base_url=sub_model_api_base, api_key=sub_model_api_key, timeout=60)
    tools = build_tools(pools)

    messages = [
        {"role": "system", "content": build_system_prompt(pools)},
        {"role": "user", "content": question},
    ]

    n_delegates = 0
    answer = None
    complete = False
    models_used = []
    skills_used = []

    for _ in range(MAX_STEPS):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                temperature=temperature, max_tokens=2048,
            )
        except Exception:
            break

        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append(msg.model_dump())
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)

                if name == "finish":
                    answer = args.get("answer", "")
                    complete = True
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps({"status": "done"})})
                    break
                elif name == "delegate_task":
                    n_delegates += 1
                    models_used.append(args.get("model", pools["models"][0]))
                    skills_used.append(args.get("skill", "direct_answer"))
                    try:
                        sub_resp = sub_client.chat.completions.create(
                            model=args["model"],
                            messages=[{"role": "user", "content": args["instruction"]}],
                            temperature=0.1, max_tokens=1024,
                        )
                        result = sub_resp.choices[0].message.content.strip()
                    except Exception as e:
                        result = f"Error: {e}"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if complete:
                break
        elif msg.content:
            messages.append({"role": "assistant", "content": msg.content})
        else:
            break

    return {"messages": messages, "answer": answer, "complete": complete,
            "n_delegates": n_delegates, "models_used": models_used, "skills_used": skills_used}

def fuzzy_match(pred: str, gold: str) -> bool:
    if not pred or not gold:
        return False
    def norm(s):
        s = s.lower().strip()
        s = re.sub(r'\b(the|a|an)\b', '', s)
        s = re.sub(r'[^\w\s]', '', s)
        return re.sub(r'\s+', ' ', s).strip()
    p, g = norm(pred), norm(gold)
    if p == g or g in p:
        return True
    try:
        return abs(float(p) - float(g)) < 1e-3
    except ValueError:
        return False

def to_sharegpt(messages: list[dict]) -> list[dict]:
    conversations = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            conversations.append({"from": "system", "value": msg["content"]})
        elif role == "user":
            conversations.append({"from": "human", "value": msg["content"]})
        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                if msg.get("content"):
                    conversations.append({"from": "gpt", "value": msg["content"]})
                for tc in tool_calls:
                    fn = tc if isinstance(tc, dict) else tc
                    call = {
                        "name": fn["function"]["name"],
                        "arguments": json.loads(fn["function"]["arguments"])
                            if isinstance(fn["function"]["arguments"], str)
                            else fn["function"]["arguments"]
                    }
                    conversations.append({"from": "function_call",
                                          "value": json.dumps(call, ensure_ascii=False)})
            else:
                conversations.append({"from": "gpt", "value": msg.get("content", "")})
        elif role == "tool":
            conversations.append({"from": "observation", "value": msg.get("content", "")})
    return conversations


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages) // 3


def router_probe(question: str, gold: str, args: argparse.Namespace, pools: PoolConfig) -> bool:
    for _ in range(ROUTER_PASS_K):
        result = run_agent(
            question,
            args.router_model,
            args.router_api_base,
            args.router_api_key,
            args.sub_model_api_base,
            args.sub_model_api_key,
            pools,
            temperature=0.7,
        )
        if result["complete"] and fuzzy_match(result["answer"], gold):
            return True
    return False


def teacher_run(question: str, gold: str, args: argparse.Namespace, pools: PoolConfig) -> tuple[bool, dict]:
    result = run_agent(
        question,
        args.teacher_model,
        args.teacher_api_base,
        args.teacher_api_key,
        args.sub_model_api_base,
        args.sub_model_api_key,
        pools,
        temperature=0.3,
    )
    return result["complete"] and fuzzy_match(result["answer"], gold), result


def filter_and_pack(task: dict, teacher_result: dict) -> tuple[bool, dict | None]:
    est_tokens = estimate_tokens(teacher_result["messages"])
    if est_tokens > MAX_TRAINING_TOKENS:
        return False, None
    return True, {
        "conversations": to_sharegpt(teacher_result["messages"]),
        "source": task["source"],
        "domain": task["domain"],
        "gold_answer": task["gold_answer"],
        "n_delegates": teacher_result["n_delegates"],
        "models_used": teacher_result["models_used"],
        "skills_used": teacher_result["skills_used"],
    }


def load_question_pool(recipe: list[dict]) -> list[dict]:
    from datasets import load_dataset

    all_tasks = []
    for entry in recipe:
        name = entry["name"]
        n = entry.get("n_samples")
        print(f"  Loading {name} (n={'all' if n is None else n})...", end=" ", flush=True)
        try:
            kw = {"split": entry["split"]}
            if n is not None:
                kw["streaming"] = True
            if "hf_subset" in entry:
                ds = load_dataset(entry["hf_path"], entry["hf_subset"], **kw)
            else:
                ds = load_dataset(entry["hf_path"], **kw)
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        filter_sources = set(entry.get("filter_sources", []))
        count = 0
        for row in ds:
            if n is not None and count >= n:
                break
            if filter_sources and row.get("source", "") not in filter_sources:
                continue
            q, gold = _extract_qa(name, row)
            if q and gold:
                all_tasks.append({"question": q, "gold_answer": gold,
                                  "source": name, "domain": entry["domain"]})
                count += 1
        print(f"{count} loaded")
    return all_tasks


def _extract_qa(name: str, row: dict) -> tuple[str, str]:
    if "numinamath" in name:
        sol = row.get("solution", "")
        m = re.findall(r'\\boxed\{([^}]+)\}', sol)
        return row.get("problem", ""), m[-1] if m else ""
    if "dapo" in name:
        prompt = row.get("prompt", [])
        return (prompt[0]["content"] if prompt else ""), row.get("reward_model", {}).get("ground_truth", "")
    if "drop" in name:
        spans = row.get("answers_spans", {}).get("spans", [])
        return f"Passage: {row.get('passage','')}\n\nQuestion: {row.get('question','')}", spans[0] if spans else ""
    if "hotpotqa" in name:
        return row.get("question", ""), row.get("answer", "")
    if "taco" in name:
        sols = row.get("solutions", "")
        return row.get("question", ""), (sols[0] if isinstance(sols, list) and sols else str(sols))
    if "leetcode" in name:
        return row.get("content", row.get("question", "")), ""
    if "toolace" in name:
        convs = row.get("conversations", [])
        q = next((c.get("value", "") for c in convs if c.get("from") == "user"), "")
        gold = next((c.get("value", "") for c in convs if c.get("from") == "assistant"), "")
        return q, gold
    return row.get("question", ""), row.get("answer", "")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", default=RECIPE_PATH)
    parser.add_argument("--pools", default=POOLS_PATH)
    parser.add_argument("--router-model", required=True)
    parser.add_argument("--router-api-base", required=True)
    parser.add_argument("--router-api-key", default="none")
    parser.add_argument("--teacher-model", default="claude-sonnet-4-6")
    parser.add_argument("--teacher-api-base", required=True)
    parser.add_argument("--teacher-api-key", required=True)
    parser.add_argument("--sub-model-api-base", required=True)
    parser.add_argument("--sub-model-api-key", default="none")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tasks", type=int, default=None)
    args = parser.parse_args()

    random.seed(args.seed)
    pools = load_pools(args.pools)
    recipe = load_recipe(args.recipe)
    print(f"Model pool: {pools['models']}")
    print(f"Skill pool: {pools['skills']}\n")

    print("Loading question pool...")
    all_tasks = load_question_pool(recipe)
    if args.max_tasks:
        random.shuffle(all_tasks)
        all_tasks = all_tasks[:args.max_tasks]
    print(f"Total: {len(all_tasks)} tasks\n")

    os.makedirs(args.out_dir, exist_ok=True)
    sft_path = os.path.join(args.out_dir, "sft.jsonl")
    rl_path = os.path.join(args.out_dir, "rl.jsonl")
    report_path = os.path.join(args.out_dir, "curriculum_report.json")

    sft_data, rl_data = [], []
    stats = {"total": len(all_tasks), "router_ok": 0, "sft": 0, "rl": 0, "overlong": 0,
             "per_source": defaultdict(lambda: {"total": 0, "router_ok": 0, "sft": 0, "rl": 0})}

    for task in tqdm(all_tasks, desc="Pipeline"):
        q, gold, src = task["question"], task["gold_answer"], task["source"]
        stats["per_source"][src]["total"] += 1

        router_ok = router_probe(q, gold, args, pools)
        if router_ok:
            stats["router_ok"] += 1
            stats["per_source"][src]["router_ok"] += 1
            continue

        teacher_ok, teacher_result = teacher_run(q, gold, args, pools)
        if not teacher_ok:
            rl_data.append({"question": q, "gold_answer": gold, "source": src, "domain": task["domain"]})
            stats["rl"] += 1
            stats["per_source"][src]["rl"] += 1
            continue

        keep_sft, packed_sample = filter_and_pack(task, teacher_result)
        if not keep_sft:
            stats["overlong"] += 1
            continue

        sft_data.append(packed_sample)
        stats["sft"] += 1
        stats["per_source"][src]["sft"] += 1

    with open(sft_path, "w") as f:
        for s in sft_data:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(rl_path, "w") as f:
        for s in rl_data:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    stats["per_source"] = {k: dict(v) for k, v in stats["per_source"].items()}
    with open(report_path, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"Total:             {stats['total']}")
    print(f"Router already OK: {stats['router_ok']} → dropped")
    print(f"SFT (teacher OK):  {stats['sft']} → {sft_path}")
    print(f"RL (teacher fail): {stats['rl']} → {rl_path}")
    print(f"Overlong dropped:  {stats['overlong']}")
    print(f"\nPer-source:")
    for src, sc in sorted(stats["per_source"].items(), key=lambda x: -x[1]["total"]):
        rok_pct = sc["router_ok"] / max(sc["total"], 1) * 100
        print(f"  {src:20s} total={sc['total']:>5d}  router_ok={sc['router_ok']:>4d} ({rok_pct:4.1f}%)  sft={sc['sft']:>4d}  rl={sc['rl']:>4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
