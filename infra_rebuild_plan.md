# SkillRouter RL Infra Rebuild Plan

## 0. Context

The previous RL stack (`scripts/rl/skillrouter_generation.py`,
`scripts/rl/reward_manager.py`, `scripts/rl/launch_grpo.py`,
`scripts/rl/run_grpo_skillrouter.sh`) has been deleted. It was a
drop-in extension of Router-R1's `LLMGenerationManager` / `RewardManager`
with a monkey-patched launcher, which conflicts with how we want the
paper to frame this work (independent, built directly on upstream verl).

Two older plan docs (`vllm_0_11_port_plan.md`, `router_r1_decouple_plan.md`)
are also deleted: the first is obsoleted by Step 1 Gate 2 PASS of the
vLLM shim work plus the rebuild now happening at a higher level; the
second is superseded by this document.

This plan rebuilds the RL stack from scratch against upstream verl
(`0.7.0.dev0` at `/home/haozy/verl`, Bytedance, Apache-2.0), with no
Router-R1 lineage.

## 1. Goal

Produce a GRPO training pipeline for SkillRouter that:
- Is expressed as a verl AgentLoop / RewardManager extension (whatever
  verl 0.7 actually calls these entry points), not a monkey-patch over
  verl internals.
- Produces RL rollout token streams byte-identical to the SkillRouter
  SFT training data (so PPO ratios are sane at step 0 against the SFT
  warm-start checkpoint at `/data/xieht/LlamaFactory/outputs/router_qwen25_7b_sft`).
- Uses the existing `agent_system/environments/env_package/skillrouter/envs.py`
  unchanged (minus the Router-R1-labelling sweep already done).
- Keeps the schema-v1.1 token protocol (`<plan>/<route>/<obs>/<verify>/<final_answer>`)
  the SFT model was trained on.
- Runs the 10-step GRPO smoke on the 4-GPU setup with the SFT checkpoint
  and advances past step 0 without NaN/inf.

## 2. What Stays / Goes / Needs Rebuild

### Stays (no change)
- `verl/` — upstream Bytedance verl at tag **v0.7.0** (commit
  `f9c855f7cf04d603c9546bc01776c74806a879c1`, cloned from
  https://github.com/verl-project/verl.git, tree flattened so the
  importable Python package is at `verl/verl/`). Apache-2.0.
  `.gitignore` whitelists it as intentional vendoring.
- `agent_system/environments/env_package/skillrouter/envs.py` — schema
  v1.1 parsers, env step/reset, sub-agent dispatch, rolling-percentile
  cost reward. All of this is our own; Router-R1 framing already
  purged.
- `scripts/rl/prepare_prompt_pool.py` — data pipeline, our own.
- `eval_pipeline/` — eval stack, already independent.
- `data/` — RL prompt pool parquet + system prompts.
- SFT checkpoint + LlamaFactory SFT pipeline — external, untouched.

### Gone (deleted in this work)
- `scripts/rl/skillrouter_generation.py`
- `scripts/rl/reward_manager.py`
- `scripts/rl/launch_grpo.py`
- `scripts/rl/run_grpo_skillrouter.sh`
- `vllm_0_11_port_plan.md`, `router_r1_decouple_plan.md`
- Old vendored `verl/` (Router-R1-era fork, self-reported version `0.1`,
  carried five hand-rolled `verl/third_party/vllm/vllm_v_0_*_*/` shims).
  Replaced wholesale with upstream v0.7.0 per Step 0 survey.
- `verl/third_party/vllm/vllm_v_0_11_0/` — our own Gate-2-PASS 0.11 shim
  is obsolete under upstream v0.7 (which does `from vllm import LLM`
  after a `>=0.7.0` version gate and has no shim API for downstream
  callers). Deleted as part of the upstream swap.

### Rebuild (new files to create)
- `scripts/rl/skillrouter_rollout.py` — the rollout manager, implemented
  as a subclass of verl's AgentLoop abstraction (exact base class TBD
  by Step 0 survey).
- `scripts/rl/skillrouter_reward.py` — reward manager implementing
  `R = (1-α)·R_outcome + α·R_cost` with cost reward sourced from the
  env step's metadata (since `envs.py` already computes it).
- `scripts/rl/train_grpo.py` — entry-point invoking `verl.trainer.main_ppo.main`
  with our components registered via verl's proper API (not monkey-patch).
- `scripts/rl/run_grpo.sh` — shell wrapper mirroring the old script's
  hyperparameters but with the new Python entry point.
- `tests/rl/test_rollout_byte_identity.py` — unit test gating every
  rollout-manager change; compares new rollout output to a golden
  token-stream fixture derived from real SFT training data.
- `tests/rl/fixtures/sft_golden_trajectory.json` — canned trajectory
  with question + policy turns + canned worker responses + expected
  token IDs.

## 3. Hard Constraints

1. **Byte-identity invariant**: new rollout produces token streams
   byte-identical to what SFT was trained on for any fixed trajectory.
   Validated by `test_rollout_byte_identity.py` before Step 6 smoke.
   Rationale: PPO ratio = 1 at step 0 when policy is the SFT init
   only if the RL-time token stream matches SFT exactly. Even a
   single-token drift blows this up silently.
2. **No monkey-patches**: `train_grpo.py` cannot do
   `verl.trainer.main_ppo.RewardManager = ...` or
   `verl.trainer.ppo.ray_trainer.LLMGenerationManager = ...`. If
   verl 0.7's API doesn't expose a proper registration path, document
   the gap in Step 0 and escalate (either PR upstream, or a minimal
   wrapper that's clearly labelled as verl plumbing, not Router-R1
   idiom).
3. **No Router-R1 references in new files**: zero hits for
   `Router-R1|drop-in|mirrors|unchanged from|Following Router-R1` in
   any file created by this plan.
4. **vLLM 0.11 shim not regressed**: Step 1 Gate 2 PASS of the 0.11
   shim remains valid. No edits to `verl/third_party/vllm/vllm_v_0_11_0/`
   during this work unless Step 0 explicitly determines the shim is
   obsoleted by upstream verl's own 0.11 support.
5. **Env code untouched**: Step 3 reward manager consumes env
   metadata; it does not add `_rolling_percentile_cost_reward` or
   equivalents back into `envs.py`. The env file is done.

## 4. Step List

### Step 0 — Upstream verl API Survey (½ day)

**Scope**: read enough of `/home/haozy/verl` (verl 0.7.0.dev0) cross-referenced
against `/data/xieht/verl-upstream` (full-history clone, tag `v0.7.0` =
`f9c855f`, main HEAD = `2239fd0`) to know what to extend.

**3 sign-off questions (must answer unambiguously):**
1. Does upstream verl 0.7 natively support the vLLM 0.11 path we need,
   or do we still carry `vllm_v_0_11_0/`?
2. How does the reward manager register with the trainer, and through
   what channel does it receive the env's `meta_info` fields
   (`data_source`, `ground_truth`, `tests`, `env_kwargs`)?
3. What is the minimum override surface on the agent-loop / rollout
   base class to implement schema-v1.1 turn orchestration?

**Deliverables (all must exist before Gate):**

- **`docs/verl_0_7_api_notes.md`** — survey notes + explicit Go/No-Go
  verdict per subsystem. See template at the file itself; required
  sections:
  - **Compat matrix** — table mapping our minimum contract (rollout
    entry, reward registration, DataProto field access, vLLM rollout
    worker hooks) to the exact verl 0.7 interface that satisfies it
    (or: "gap"). Rows: rollout / reward / trainer / vLLM worker / data
    flow. Columns: our-contract-needs, verl-0.7-surface, file:line in
    `/home/haozy/verl`, Go/No-Go.
  - **Go/No-Go rule** (applied to each row):
    - **Go** = native verl 0.7 support, no monkey-patch required.
    - **No-Go** = missing hook; must list the gap, patch cost (upstream
      PR vs. vendored shim vs. thin wrapper), and whether it blocks
      the rebuild.
  - **Delete/Keep list** — evidence-based decision for
    `vllm_v_0_11_0/` (and any other vendored dir under
    `verl/third_party/`). Cite the verl 0.7 file that supersedes (or
    doesn't) our shim. No preference-based keeps.
  - **Minimum pseudocode skeleton** — for each of the 5 new files
    (`skillrouter_rollout.py`, `skillrouter_reward.py`, `train_grpo.py`,
    `run_grpo.sh`, `tests/rl/test_rollout_byte_identity.py`), show the
    call-order stub: imports, base class / decorator, method signatures
    invoked by verl, and the order in which verl calls them during a
    single training step. Stubs only; no real logic.

**Gate**: user reads `docs/verl_0_7_api_notes.md`, signs off that all
3 questions are answered, compat matrix has no open rows, delete/keep
call is backed by cited evidence, pseudocode skeletons match the
compat-matrix contract. Only then does Step 2 start.

**Commit**: `infra-rebuild: upstream verl 0.7 API survey`

### Step 1 — Byte-Identity Test Harness (½ day)

**Scope**: pin the token-stream invariant in test form before any new
rollout code lands, so every subsequent commit can be gated against it.

- Pick 1-2 trajectories from real SFT training data (whatever file
  LlamaFactory trained on — Step 0 locates it).
- Golden fixture = `(question, system_prompt, policy_turns[], canned_worker_responses[], expected_token_ids[])`.
- Test: feed `question + system_prompt` to a placeholder rollout
  manager (stub returning `policy_turns[i]` on turn `i`, using
  `canned_worker_responses[i]` from `envs.py`), assert the final
  concatenated token stream equals `expected_token_ids`.
- At Step 1, the placeholder passes trivially. The real value lands in
  Step 2 when the new rollout manager replaces the placeholder.

**Gate**: `pytest tests/rl/test_rollout_byte_identity.py -v` passes on
the placeholder.

**Commit**: `infra-rebuild: rollout byte-identity test harness + SFT golden fixture`

### Step 2 — New Rollout Manager (2-3 days)

**Scope**: write `scripts/rl/skillrouter_rollout.py` as a subclass of
verl 0.7's rollout-manager base (identified in Step 0).

- schema v1.1 parsing (reuse `PLAN_RE`/`ROUTE_RE`/`FINAL_RE` from
  `envs.py` via a small shared module, don't duplicate).
- Turn orchestration: prompt → policy generation → env step → obs
  injection → next policy turn. Bounded by `max_turns`.
- Token stream assembly: use verl's helpers where they exist; write
  our own where they don't. The implementation is derived from verl's
  conventions, not from the deleted file.
- Sub-agent worker-pool dispatch stays in `envs.py` — the rollout
  manager only sees obs strings returned by env.step().
- **No Router-R1 references anywhere in the file.**

**Gate 2.1**: byte-identity test now runs against the real rollout
manager and passes.

**Gate 2.2**: `grep -rn "Router-R1\|drop-in\|mirrors\|unchanged from"
scripts/rl/skillrouter_rollout.py` = zero.

**Commit**: `infra-rebuild: SkillRouter rollout manager on verl 0.7 agent API`

### Step 3 — New Reward Manager (½ day)

**Scope**: write `scripts/rl/skillrouter_reward.py`.

- Take the terminal-step reward from `envs.py` (already
  `(1-α)·correctness + α·cost`).
- Integrate with verl 0.7's RewardManager protocol (from Step 0).
- Handle data-source-specific weighting if the trainer needs it.
- **Rolling-percentile math stays in `envs.py`**. Reward manager is
  just an adapter between env output and verl's reward tensor.

**Gate 3**: reward tensor values on a fixed mini-batch are
bit-identical whether computed inline or through this manager.

**Commit**: `infra-rebuild: reward manager adapter (env-first reward composition)`

### Step 4 — New Launcher + Shell Wrapper (½ day)

**Scope**: write `scripts/rl/train_grpo.py` and
`scripts/rl/run_grpo.sh`.

- `train_grpo.py`: path setup + register SkillRouter rollout manager
  and reward manager with verl via the proper API (from Step 0) +
  invoke `verl.trainer.main_ppo.main` (or equivalent 0.7 entry point).
- The `DataProto.pop` fix for preserving `env_kwargs` / `reward_model`
  / `data_source` / `extra_info` across pops: if verl 0.7 still has
  the same gap, file an upstream issue or re-apply the fix with a
  clear comment labelling it a verl-upstream workaround (not a
  Router-R1 pattern). If verl 0.7 has fixed it, drop the workaround.
- `run_grpo.sh`: same hyperparameter block as the deleted script
  (group_size=8, max_turns=5, alpha_init=0.1, etc.) but invoking
  `train_grpo.py`.

**Gate 4**: `bash scripts/rl/run_grpo.sh` starts the trainer and loads
batch 0 without crash.

**Commit**: `infra-rebuild: GRPO launcher + shell wrapper`

### Step 5 — Textual Sweep (½ day)

**Scope**: remaining Router-R1 references outside prior-art contexts.

- `README.md`: rewrite the architecture section that currently
  describes the deleted `SkillRouterGenerationManager` as a "drop-in
  replacement for Router-R1's `LLMGenerationManager`"; describe the
  new verl-native architecture. Keep Router-R1 in the baselines
  comparison table as legitimate prior art.
- Double-check `eval_pipeline/routers/base.py:2` and
  `scripts/run_full_eval.sh:52` read as prior-art references, not
  inheritance claims.

**Gate 5**: `grep -rn "Router-R1\|drop-in\|mirrors\|unchanged from\|Following Router-R1\|Router-R1 convention\|Router-R1 style" --include='*.py' --include='*.md' --include='*.sh'` returns only:
- `README.md` baselines comparison table
- `eval_pipeline/routers/base.py:2` (example list of concrete routers)
- `scripts/run_full_eval.sh:52` (baseline checkpoint registration example)
- `infra_rebuild_plan.md` (this file)

**Commit**: `infra-rebuild: README + residual docstring sweep`

### Step 6 — 10-Step GRPO Smoke (1 day)

**Scope**: end-to-end validation on the 4-GPU setup.

- Run `bash scripts/rl/run_grpo.sh` for 10 steps on a small batch
  with the SFT warm-start checkpoint.
- Expected: trainer advances past step 0, PPO ratio ≈ 1.0 on step 0,
  no NaN/inf, reward values in `[0, 1]`, at least some non-zero
  rewards on atomic-QA sources.
- If step 0 ratio drifts: byte-identity test was not strict enough;
  diagnose with a forward-pass spot check on a fixed trajectory.

**Gate 6**: 10 steps complete, wandb trace looks sane, no crash.

**Commit**: `infra-rebuild: 10-step GRPO smoke PASS on 4-GPU setup`

## 5. Sequencing with vLLM 0.11 Shim Work

The 0.11 shim is at Step 1 Gate 2 PASS. Subsequent shim steps
(output-contract parity, sleep/wake, weight sync, etc.) are still
required for the 10-step smoke to work end-to-end.

**Proposed order**:
- Infra rebuild Steps 0-5 first (gets the Python-side infra clean and
  ready).
- vLLM shim Steps 2-4.5 interleaved (do vLLM Step 2 before infra
  Step 6, so the smoke has a stable rollout engine).
- Infra Step 6 (10-step smoke) is the joint acceptance gate for both
  workstreams.

**Alternative**: if Step 0 reveals verl 0.7 natively supports vLLM 0.11
(and our `vllm_v_0_11_0/` shim becomes obsolete), delete the shim and
skip all remaining vLLM shim steps. Decision at Step 0.

## 6. Risks

1. **verl 0.7 agent API not stable / well documented**. Verl is
   fast-moving pre-1.0 software; Step 0 may find the API is partly
   experimental (`verl.experimental.agent_loop`) and subject to
   change. Mitigation: pin to a specific verl commit in our setup,
   and put the agent-API version in `docs/verl_0_7_api_notes.md` so we
   notice if upstream shifts.
2. **Byte-identity test is the only real correctness gate before
   smoke**. If we pick the wrong golden trajectory (e.g. one that
   happens to avoid a tricky obs-injection edge case), the test
   passes but the real run diverges. Mitigation: pick at least 2
   trajectories covering (a) zero-route lazy mode on `gsm8k`, (b)
   multi-route plan on `hotpotqa`, (c) code task on `taco` if tests
   are in the prompt pool.
3. **verl 0.7's `DataProto.pop` may still drop `env_kwargs`**. The
   deleted launcher had a workaround for this. If verl 0.7 still has
   the gap and we drop the workaround, env.reset() gets empty kwargs
   and rewards are all 0. Mitigation: Step 0 checks this; Step 4
   re-applies workaround labelled as verl workaround if needed.
4. **SFT training data may not be available on this host**. The SFT
   run was done with LlamaFactory at a likely different path.
   Mitigation: Step 0 locates the data; if unavailable, reconstruct
   the golden fixture from `data/rl/train.parquet` + the SFT system
   prompt instead.

## 7. Acceptance

All 6 gates pass. `grep -rn Router-R1 . --include='*.py' --include='*.md' --include='*.sh'`
returns only the documented prior-art references (README baselines
table, eval baseline registration example, this plan file). 10-step
GRPO smoke runs clean on the 4-GPU SFT warm-start. At that point the
paper can honestly state: "We build directly on verl (upstream
Bytedance). Router-R1 appears in our experiments as one of several
prior-art baseline routers."
