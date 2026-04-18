"""Re-run all 50 samples with updated prompts and model descriptions."""
import sys, os, json, asyncio, time, logging
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../..")))

# Clear config cache so new pools.yaml is loaded
import configs
configs._CACHE_BY_PATH.clear()

from configs import load_pools
from scripts.data.agent import arun_agent
from scripts.data.verifiers import verify

logging.basicConfig(level=logging.WARNING)

PLANNER_MODEL = "Qwen/Qwen2.5-7B-Instruct"
PLANNER_API_BASE = "http://localhost:8234/v1"
ROUTER_MODEL = "Qwen/Qwen2.5-7B-Instruct"
ROUTER_API_BASE = "http://localhost:8234/v1"
SUB_MODEL_API_BASE = "https://open.xiaojingai.com/v1/"
SUB_MODEL_API_KEY = "sk-Koqmqsvz6RO0ptLNuXRsZRXnzSPdZzkrqr6FL4M5HcbvMS4Q"

TRAJ_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../data/trajectories")
IN_PATH = os.path.join(TRAJ_DIR, "infra_test_50_clean.jsonl")
OUT_PATH = os.path.join(TRAJ_DIR, "infra_test_50_v4.jsonl")
POOLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../configs/pools.yaml")
CONCURRENCY = 16


async def run_one(s, pools, sem, idx, total):
    async with sem:
        t0 = time.time()
        try:
            result = await arun_agent(
                question=s["question"],
                planner_model=PLANNER_MODEL, planner_api_base=PLANNER_API_BASE, planner_api_key="none",
                router_model=ROUTER_MODEL, router_api_base=ROUTER_API_BASE, router_api_key="none",
                sub_model_api_base=SUB_MODEL_API_BASE, sub_model_api_key=SUB_MODEL_API_KEY,
                pools=pools,
            )
        except Exception as e:
            result = {"answer": None, "complete": False, "n_delegates": 0,
                      "models_used": [], "skills_used": [], "routing_decisions": [],
                      "subtasks": [], "messages": [], "error": str(e)}

        answer = result.get("answer")
        correct = verify(answer, s["gold_answer"], s["source"]) if answer is not None else False
        dt = time.time() - t0
        models = result.get("models_used", [])

        status = "OK" if correct else ("WRONG" if result.get("complete") else "INCOMPLETE")
        print("[%d/%d] tid=%d %s ans=%s gold=%s model=%s (%.1fs)" % (
            idx + 1, total, s["task_id"], status,
            str(answer)[:25], str(s["gold_answer"])[:25],
            models[0] if models else "none", dt))

        return {
            "task_id": s["task_id"], "source": s["source"], "domain": s["domain"],
            "question": s["question"], "gold_answer": s["gold_answer"],
            "answer": answer, "correct": correct, "complete": result.get("complete", False),
            "n_delegates": result.get("n_delegates", 0),
            "models_used": result.get("models_used", []),
            "skills_used": result.get("skills_used", []),
            "routing_decisions": result.get("routing_decisions", []),
            "subtasks": result.get("subtasks", []),
            "messages": result.get("messages", []),
        }


async def main():
    pools = load_pools(POOLS_PATH)

    with open(IN_PATH) as f:
        samples = [json.loads(l) for l in f]

    print("Re-running %d samples (v3: updated prompts + model descriptions)\n" % len(samples))
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()

    tasks = [run_one(s, pools, sem, i, len(samples)) for i, s in enumerate(samples)]
    results = await asyncio.gather(*tasks)

    with open(OUT_PATH, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    complete = sum(1 for r in results if r["complete"])

    # Routing distribution
    from collections import Counter
    model_counts = Counter()
    for r in results:
        for m in r.get("models_used", []):
            model_counts[m] += 1

    print("\n" + "=" * 50)
    print("RESULTS: %d/%d correct (%.0f%%), %d/%d complete" % (
        correct, total, 100 * correct / total, complete, total))
    print("Time: %.0fs" % (time.time() - t0))
    print("\nRouting distribution:")
    for m, cnt in model_counts.most_common():
        print("  %-25s %d" % (m, cnt))
    print("\nSaved: %s" % OUT_PATH)


if __name__ == "__main__":
    asyncio.run(main())
