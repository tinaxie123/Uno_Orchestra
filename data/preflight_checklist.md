# Pre-Flight Checklist (LOCKED)

**Status**: Locked. This is the gating document between writing code and spending real money on full-scale distillation. No deviations without an explicit decision logged in the changelog of `experiment_plan_v3.md`.

**Hard rule**: Full-scale Phase 1 distillation (~$1,800–$2,200, ~41.5k samples) **may not start** until every Go/No-Go gate below has been passed in order.

---

## Phase A — Pre-production validation

Local code only. No GPU spend. API spend < $5.

### A1. Lock `config/pools.yaml`
- ✅ DONE — 7 executors + 6 skills + `policy_model` + `enforcement: strict`.
- Verification: `python3 scripts/schema_validator.py` prints `loaded pools from .../pools.yaml: 7 models, 6 skills` and `ALL PASS`.

### A2. Lock `config/sft_recipe.yaml`
- Codify `experiment_plan_v3.md §2.1` into a single YAML file.
- Each entry: `name`, `hf_path`, `hf_subset` (if any), `split`, `n_samples`, `domain`, `difficulty`, `distill_model`.
- Total samples must equal 41,500 ± 100.
- **Gate**: `python3 -c "import yaml; d=yaml.safe_load(open('config/sft_recipe.yaml')); assert sum(e['n_samples'] for e in d['datasets']) == 41500"` returns 0.

### A3. Run `scripts/availability_probe.py`
For every dataset in the recipe, verify in order:

1. `datasets.load_dataset(hf_path, hf_subset, split=split, streaming=True)` succeeds without auth.
2. The first sample is fetchable.
3. The required fields (question / answer / context / etc., per dataset) are present and non-empty.
4. The split contains ≥ `n_samples` examples (or document if streaming-only).

**Gate (A3)**: ≥ 90% of datasets pass. Failures are logged and either:
- (a) replaced with a Phase 2 deferred dataset, **or**
- (b) downgraded to streaming + best-effort sampling, **or**
- (c) removed and the deficit absorbed by upweighting an adjacent dataset in the same domain (must keep total = 41,500 ± 100).

The failure list and resolution is appended to `experiment_plan_v3.md §7 changelog` before continuing.

### A4. Write `scripts/distill.py` (Phase A subset)
At minimum support:
- Single sample generation against the xiaojingai endpoint (or any OpenAI-compatible endpoint via `OPENAI_BASE_URL` / `OPENAI_API_KEY`).
- Distillation prompt enforces schema v1.1 §7 rules.
- Output goes through `validate_messages()` from `scripts/schema_validator.py`; failures are retried up to 3 times.
- JSONL append-only output to `data/sft/dryrun.jsonl`, with one extra column per row containing the validator's structured `stats` for downstream analysis.
- Cost meter: track input/output tokens per sample, accumulate per-domain totals.
- Checkpoint resume: re-running the script skips ids already present in the output file.

**Gate (A4)**: smoke run on 3 samples (1 lazy, 1 multi-hop, 1 code) all pass `validate_messages()` after at most 1 retry.

### A5. Run 30-sample dry run
- Sampling rule: **stratified by domain, NOT random**. A random 30 will under-cover small domains (logic, long context, domain knowledge) and over-cover large ones (multi-hop, single-hop). The whole point of A5 is to surface per-domain failures, which random sampling defeats. At least:
  - 4 multi-hop QA
  - 3 single-hop / lazy
  - 3 math
  - 3 code
  - 3 STEM
  - 3 commonsense
  - 2 formal logic
  - 2 long context
  - 2 domain knowledge (med/law/finance)
  - 3 tool / agent
  - 2 free slots for under-represented categories
- Total: 30 samples.
- Each sample passes through `distill.py` → `validate_messages()` → JSONL.

#### Gate A5 — Go criteria for the 30-sample run

| Metric | Threshold | Rationale |
|---|---|---|
| Schema valid rate (first attempt) | ≥ 90% | teacher follows schema reliably |
| Schema valid rate (after ≤ 3 retries) | ≥ 95% | retry budget is sufficient |
| Repair sample fraction | ≥ 15% | repair branch has training signal |
| Lazy sample fraction | > 0% | collapse path is reachable |
| Distinct executor models used | ≥ 5 of 7 | router is not collapsing to one model |
| Distinct skills used | ≥ 4 of 6 | skill diversity is reachable |
| (model, skill) pair entropy | ≥ 2.5 nats | no extreme pair concentration |
| Manual review pass rate | ≥ 28 / 30 | semantic quality is acceptable |

Concrete failure modes to look for in the manual review (any single one fails A5):
- ≥ 95% of routes use a single model (e.g. teacher always picks `claude-opus-4-6`)
- `code_exec` skill never used despite math/code samples in the dry run
- `claude-haiku-4-5-20251001` either never used or used > 70% of routes
- Some skill in the pool (e.g. `table_ops`, `browser_use`) appears 0 times
- A specific tag (e.g. `<verify>`, `depends_on`) is wrong in > 1/3 of samples — indicates a prompt bug, not a teacher quirk

**If any threshold fails**: do NOT proceed to Phase B. Iterate on the distillation prompt, re-run the 30-sample dry run. Up to 3 prompt iterations are allowed before escalating to a recipe re-design.

---

## Phase B — Pilot batch (300–500 samples)

Triggered only after Phase A passes every gate.

### B1. Stratified sample of 400
- Same stratification ratios as A5, scaled up to 400.
- Run through `distill.py` end-to-end with full retry, validator filtering, and cost meter on.

#### Gate B1 — Go criteria for the 400-sample pilot

| Metric | Threshold | Rationale |
|---|---|---|
| Schema valid rate (final, after retries) | ≥ 95% | stability holds at 10× scale |
| Per-domain valid rate | ≥ 85% in every domain | no domain is silently broken |
| Repair sample fraction | 15–40% | not too few, not flooded |
| Lazy sample fraction | 10–30% | within design intent |
| Cost projection vs. plan | within ±25% of $1,800–$2,200 | budget is realistic |
| Manual review pass rate | ≥ 90% (random 50 samples) | semantic quality holds |
| Per-domain (model, skill) distribution | no single pair > 60% within a domain | router signal is preserved |

**If any threshold fails**: do NOT proceed to Phase C. Either iterate the prompt, re-stratify the recipe, or split a problematic domain into Phase 2.

---

## Phase C — Full-scale Phase 1 distillation (~41.5k)

Triggered only after Phase B passes every gate.

### C1. Pre-launch sanity
- Capacity check: estimate wall-clock time given Phase B's tokens/sample average and the API rate limit; confirm < 72 hours.
- Cost check: estimate total dollars from Phase B's token average; confirm within ±15% of plan.
- Disk check: estimate parquet size; confirm < 50% of available local disk.
- Idempotency check: re-running `distill.py` on a partially completed output file resumes correctly without duplicates.

### C2. Launch
- Run `distill.py` with the full recipe.
- Monitor every 1k samples: schema valid rate, per-domain progress, cost so far.
- **Abort criteria**: if at any 1k checkpoint the schema valid rate drops below 90%, halt and investigate.

### C3. Post-distillation decontamination
- Run 13-gram match against the union of all eval-only benchmarks listed in `experiment_plan_v3.md §1.5`.
- Drop matching samples; report removed counts per dataset.
- **Gate**: total drop rate < 5% across the full pool. If higher, investigate the offending dataset.

### C4. Final SFT pool sanity
- `validate_messages()` over the entire final parquet: 100% pass.
- Per-domain count within 5% of recipe target.
- Aggregate stats logged to `data/sft/final_stats.json`.

---

## Estimated cost ladder (cumulative)

| Phase | Samples | API spend | Wall clock |
|---|---|---|---|
| A4 smoke | 3 | < $0.50 | minutes |
| A5 dry run | 30 | < $5 | < 1 hour |
| B1 pilot | 400 | $20–$50 | a few hours |
| C2 full | ~41,500 | $1,800–$2,200 | 2–3 days |

**The point of A and B is to spend < $60 to de-risk a $2k spend.** If A or B fails, we have lost almost nothing.

---

## Implementation order (locked)

This order is the binding sequence; do not reorder without amending this document.

1. ✅ `data/schema_v1_1.md` (LOCKED)
2. ✅ `experiment_plan_v3.md` (LOCKED)
3. ✅ `config/pools.yaml` (LOCKED)
4. ✅ `scripts/schema_validator.py` (strict mode)
5. **THIS FILE** (`data/preflight_checklist.md`, LOCKED)
6. `config/sft_recipe.yaml` ← next
7. `scripts/availability_probe.py`
8. `scripts/distill.py`
9. Phase A5: 30-sample dry run + manual review
10. (gate A5 pass) Phase B1: 400-sample pilot
11. (gate B1 pass) Phase C2: full Phase 1 distillation
12. Phase C3: decontamination
13. Phase C4: final stats
14. SFT warm-start training
15. RL env + GiGPO config
16. RL training
17. Eval

Steps 6–8 are pure local code, no API spend.
Step 9 is the first API spend (< $5).
Step 11 is the first significant API spend (~$2k).
Step 16 is the first significant GPU spend.

---

## Changelog

- **v1** (this document): Initial lock. Phase A/B/C structure, all Go/No-Go thresholds, cost ladder, implementation order.
