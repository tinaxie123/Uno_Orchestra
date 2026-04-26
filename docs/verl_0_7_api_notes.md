# verl 0.7 API Survey Notes

**Status**: 🟢 Step 0 survey complete — awaiting Go/No-Go sign-off.

**Sources surveyed**
- `/data/xieht/verl-upstream` — full-history clone (tag `v0.7.0` =
  `f9c855f7cf04d603c9546bc01776c74806a879c1`, main HEAD = `2239fd0`,
  2405 commits). Survey targets v0.7.0 tag unless noted.
- `/home/haozy/verl` — **permission denied** on the survey host
  (`xieht` cannot read that tree). Not a blocker — we have a full
  clone of upstream at the same tag and more.
- `/data/xieht/multiagentRL/verl/` — our **vendored** copy in-repo.
  Findings on this tree drive the largest decision below.

**Our project commit at survey time**: `8af1be6` (main).

---

## 0. Executive Summary

| Question | Verdict | 1-line answer |
|---|---|---|
| Q1: Native vLLM 0.11 under verl 0.7? | 🟢 **Go** | Yes. Verl 0.7 `__init__.py` just does `from vllm import LLM` after a version gate (`>=0.7.0`). vLLM 0.11.x is a drop-in. |
| Q2: Reward manager registration + meta_info plumbing? | 🟢 **Go** | `@register("name")` decorator over `AbstractRewardManager.__call__(self, data: DataProto, return_dict=False)`. Fields reach us via `data.non_tensor_batch` (`reward_model`, `extra_info`, `data_source`). |
| Q3: Agent-loop override surface? | 🟢 **Go** | Subclass `AgentLoopBase`, implement one method `async def run(self, sampling_params, **kwargs) -> AgentLoopOutput`. Register with `@register("name")`. |

**Aggregate verdict**: 🟢 **Go**, but with one material structural
decision to sign off before Step 1:

> The **vendored `verl/` in our repo is not upstream v0.7** — it is an
> earlier fork that still carries Router-R1-era vendored vLLM shims
> (`vllm_v_{0_3_1, 0_4_2, 0_5_4, 0_6_3, 0_11_0}`). Upstream v0.7.0
> dropped all of those and just does `from vllm import LLM`. Our 5
> new files land cleanly against the upstream API — but only if we
> stop extending the vendored tree and switch to upstream. See §3.

---

## 1. Sign-off Questions

### Q1 — vLLM 0.11 native path

> Does upstream verl 0.7 natively support the vLLM 0.11 rollout path
> we need, or must we keep `verl/third_party/vllm/vllm_v_0_11_0/`?

**Answer**: 🟢 **verl 0.7 natively supports vLLM 0.7.0+, including
0.11.x**. The `vllm_v_0_11_0/` shim is **obsolete** under upstream
0.7 and should be deleted.

**Evidence** (`/data/xieht/verl-upstream/verl/third_party/vllm/__init__.py`,
64 lines total):

```python
if vs.parse(package_version) >= vs.parse("0.7.0"):
    vllm_version = package_version
    if vs.parse(package_version) >= vs.parse("0.8.5"):
        VLLM_SLEEP_LEVEL = 2        # 0.11.x hits this branch
    from vllm import LLM
    from vllm.distributed import parallel_state
```

- Hard floor: vllm >= 0.7.0, else `ValueError`.
- Sleep-level: 1 for 0.7.0–0.8.4, 2 for 0.8.5+. vLLM 0.11.x → level 2.
- Explicit rejection: `0.5.4` and `0.6.3` are hard errors — **the pip
  version currently installed on this host is `vllm==0.6.3`**, which
  upstream v0.7 refuses.

**Installed versions on this host (evidence for the migration):**
- `pip show verl` → `Version: 0.1` (this is the vendored in-repo tree
  self-reporting; `verl/version/version` literally contains the
  string `0.1`).
- `pip show vllm` → `Version: 0.6.3`.
- `python -c "import verl; print(verl.__file__)"` → our in-repo
  `verl/__init__.py`, not a pip-installed copy.

**Implication**: to adopt upstream v0.7, we need:
1. Install verl v0.7.0 from `/data/xieht/verl-upstream` (editable or
   pip install `.` at the v0.7.0 tag) **instead of** importing the
   vendored tree.
2. Upgrade vllm to 0.11.x.
3. Delete the vendored `verl/third_party/vllm/vllm_v_*/` shims.

### Q2 — Reward manager registration + meta_info plumbing

> How does the reward manager register with verl 0.7's trainer, and
> through what channel does it receive the env's meta_info fields?

**Answer**: 🟢 Decorator-based registry. Abstract base exposes
`DataProto` directly, so `reward_model`, `extra_info`, and
`data_source` flow through `data.non_tensor_batch` unchanged from
what `prepare_prompt_pool.py` writes into parquet.

**Evidence**:

`verl/workers/reward_manager/registry.py:23`:
```python
REWARD_MANAGER_REGISTRY: dict[str, type[AbstractRewardManager]] = {}

def register(name: str):
    def decorator(cls):
        REWARD_MANAGER_REGISTRY[name] = cls
        return cls
    return decorator

def get_reward_manager_cls(name: str) -> type[AbstractRewardManager]: ...
```

`verl/workers/reward_manager/abstract.py:23`:
```python
class AbstractRewardManager(ABC):
    @abstractmethod
    def __init__(
        self, tokenizer, num_examine,
        compute_score: RawRewardFn | None,
        reward_fn_key: str = "data_source",
        **kwargs,
    ): ...

    @abstractmethod
    def __call__(
        self, data: DataProto, return_dict: bool = False,
    ) -> torch.Tensor | dict[str, Any]: ...
```

**DataProto contract** (`verl/protocol.py:313`):
- `batch: TensorDict` — token tensors.
- `non_tensor_batch: dict` — per-sample Python objects; this is
  where `reward_model={"ground_truth": ...}`, `extra_info={...}`,
  `env_kwargs`, and `data_source` live (packed by our
  `prepare_prompt_pool.py`).
- `meta_info: dict` — shared across the batch.

**`DataProto.pop` env_kwargs concern from old plan**: not observed as
a functional gap in v0.7. The `pop` helpers (`protocol.py:215`,
`:236`) preserve `non_tensor_batch` by copy through the returned
`DataProto`. The old workaround from the deleted launcher is not
needed under 0.7.

### Q3 — Agent-loop / rollout override surface

> What is the minimum override surface to implement schema-v1.1 turn
> orchestration?

**Answer**: 🟢 `AgentLoopBase` with exactly one abstract method.

**Evidence** (`verl/experimental/agent_loop/agent_loop.py:284`):

```python
class AgentLoopBase(ABC):
    def __init__(self, trainer_config, server_manager, tokenizer,
                 processor, dataset_cls, data_config, **kwargs): ...
    async def apply_chat_template(self, messages, tools=None, ...): ...
    async def process_vision_info(self, messages): ...

    @abstractmethod
    async def run(self, sampling_params: dict[str, Any], **kwargs
                  ) -> AgentLoopOutput:
        """kwargs = dataset row fields from RLHFDataset."""
```

**Registration** (line 429):
```python
@register("uno")
class UnoAgentLoop(AgentLoopBase):
    async def run(self, sampling_params, **kwargs) -> AgentLoopOutput: ...
```

**Output schema** (`AgentLoopOutput`, line 188):
- `prompt_ids`, `response_ids`, `response_mask` (1 for LLM-generated,
  0 for tool / obs tokens — this is our plan/route/obs/verify split).
- `num_turns`, `metrics`, `extra_fields`, optional `reward_score`.

**Turn-loop reference** — the closest concrete example is
`verl/experimental/agent_loop/tool_agent_loop.py` (414 lines); our
Uno schema v1.1 fits exactly the same shape: generate →
parse → call env / worker → inject obs → next generate, bounded by
`max_turns`, with `response_mask=0` over obs spans.

**Dataset-row → run(kwargs) flow**:
- `RLHFDataset.__getitem__` returns a dict with `raw_prompt`,
  `extra_info`, `reward_model`, `data_source`, plus any extra columns
  we packed. Keys surface directly as `kwargs` in `run(...)`.
- For Uno, `kwargs["extra_info"]` already contains our
  `{question, gold, source, tests?}` bundle; `kwargs["reward_model"]`
  has `{"ground_truth": ...}`. Nothing extra to wire.

---

## 2. Compat Matrix

| Our contract | verl 0.7 surface | Evidence (`/data/xieht/verl-upstream`) | Verdict |
|---|---|---|---|
| Custom multi-turn rollout manager | `AgentLoopBase`, `@register("name")` | `verl/experimental/agent_loop/agent_loop.py:284`, `:429` | 🟢 Go |
| Reward manager registration | `AbstractRewardManager`, `@register("name")` | `verl/workers/reward_manager/registry.py:23`, `abstract.py:23` | 🟢 Go |
| `data_source`/`ground_truth`/`tests`/`env_kwargs` to reward | `DataProto.non_tensor_batch` | `verl/protocol.py:313-327`; keys flow through from parquet unchanged | 🟢 Go |
| `DataProto.pop` preserves env-side fields | `non_tensor_batch` is carried on every pop variant | `verl/protocol.py:211-238` | 🟢 Go (the old workaround is unnecessary) |
| vLLM 0.11 worker (sleep/wake, V1 engine) | Direct `from vllm import LLM` after version gate | `verl/third_party/vllm/__init__.py:43-52` | 🟢 Go, after upgrading installed vllm 0.6.3 → 0.11.x |
| GRPO entrypoint accepts custom rollout + reward | `verl.trainer.main_ppo.main` via Hydra config fields | `verl/trainer/main_ppo.py:1-30`; `reward_model.reward_manager=<name>`, `actor_rollout_ref.rollout.agent.agent_name=<name>` | 🟢 Go |
| Tokenizer + chat template for byte-identity | `AgentLoopBase.apply_chat_template()` delegates to HF `AutoTokenizer.apply_chat_template` | `verl/experimental/agent_loop/agent_loop.py:339-405` | 🟢 Go (golden fixture must use same tokenizer + `chat_template.jinja` the SFT used) |

No 🟡 rows. No 🔴 rows. All contract needs satisfied natively — under
upstream verl 0.7. (Under our vendored verl: not the case; see §3.)

---

## 3. Delete / Keep List (evidence-based)

The single biggest finding is the delta between our vendored `verl/`
and upstream v0.7.

### 3.1 Vendored `verl/` (the whole in-repo tree at `/data/xieht/multiagentRL/verl/`)

**Decision**: 🗑️ **DELETE** (replace with `pip install` from
`/data/xieht/verl-upstream` pinned at v0.7.0 tag `f9c855f`).

**Evidence**:
- `verl/version/version` → `0.1` (local tree self-reports 0.1, not
  0.7). Upstream `v0.7.0:verl/version/version` → `0.7.0.dev`.
- The vendored tree carries `verl/third_party/vllm/vllm_v_0_3_1/`,
  `vllm_v_0_4_2/`, `vllm_v_0_5_4/`, `vllm_v_0_6_3/`, `vllm_v_0_11_0/`.
  Upstream v0.7.0 carries only `verl/third_party/vllm/__init__.py`
  (single file). `git ls-tree v0.7.0 verl/third_party/vllm/` confirms.
- The multi-shim pattern is **Router-R1-era scaffolding**, not
  upstream. Keeping the vendored tree keeps that scaffolding —
  exactly what the rebuild was supposed to purge. Carrying it also
  means new code would target a 0.1-era API that doesn't have
  `AgentLoopBase`, `AbstractRewardManager`, or the native vLLM 0.11
  import path.
- Our 5 new files — `uno_rollout.py`, `uno_reward.py`,
  `train_grpo.py`, `run_grpo.sh`, `test_rollout_byte_identity.py` —
  cannot work against this vendored tree. They need the upstream v0.7
  API.

**Action**: in Step 1 (before any new code), remove vendored tree,
install upstream v0.7 editable. Plan:
1. `pip install -e /data/xieht/verl-upstream@v0.7.0` (or via a pinned
   setup; treat `/data/xieht/verl-upstream` as the source of truth).
2. `git rm -r verl/` in our repo.
3. Update `.gitignore` to stop whitelisting the in-repo tree.
4. Upgrade `vllm` to 0.11.x in `pyproject.toml`.

### 3.2 `verl/third_party/vllm/vllm_v_0_11_0/` (our recent Gate-2-PASS work)

**Decision**: 🗑️ **DELETE**.

**Evidence**:
- Verl 0.7 does `from vllm import LLM` directly after a version gate
  (`verl/third_party/vllm/__init__.py:43`). No shim API (`V_0_X_X.llm`
  / `V_0_X_X.parallel_state`) exists upstream.
- The Gate-2-PASS milestone was valid *under the vendored 0.1-era
  tree where a shim was required*. Once the vendored tree is deleted
  and we depend on upstream 0.7, the shim has no caller.
- This deletes ~weeks of our own shim work — but the work was
  specifically to bridge an obsolete fork we're now abandoning.

**Action**: deleted alongside §3.1.

### 3.3 Other in-repo dirs

| Path | Decision | Why |
|---|---|---|
| `agent_system/environments/env_package/uno/envs.py` | ✅ KEEP | Our own env + reward math. Independent of verl API. |
| `scripts/rl/prepare_prompt_pool.py` | ✅ KEEP | Data pipeline, writes parquet in verl's row convention. |
| `eval_pipeline/` | ✅ KEEP | Independent. |
| `data/` | ✅ KEEP | RL parquets. |
| SFT checkpoint + LlamaFactory pipeline | ✅ KEEP (external) | Unaffected. |

---

## 4. Minimum Pseudocode Skeletons

Concrete now that the base classes are identified.

### 4.1 `scripts/rl/uno_rollout.py`

```python
from typing import Any
from uuid import uuid4
from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase, AgentLoopOutput, register,
)
from verl.workers.rollout.replica import TokenOutput

from agent_system.environments.env_package.uno.envs import (
    UnoEnv, PLAN_RE, ROUTE_RE, FINAL_RE,
)

@register("uno")
class UnoAgentLoop(AgentLoopBase):
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        # Fields packed by prepare_prompt_pool.py:
        extra_info  = kwargs.get("extra_info") or {}
        env_kwargs  = extra_info.get("env_kwargs") or kwargs.get("env_kwargs") or {}
        raw_prompt  = list(kwargs["raw_prompt"])

        env = UnoEnv(**env_kwargs)          # local env instance per episode
        obs0 = env.reset()                          # not used if prompt already has question

        prompt_ids   = await self.apply_chat_template(raw_prompt)
        response_ids, response_mask = [], []
        num_turns    = 0

        for turn in range(self.rollout_config.multi_turn.max_turns or 5):
            out: TokenOutput = await self.server_manager.generate(
                request_id=uuid4().hex,
                prompt_ids=prompt_ids + response_ids,
                sampling_params=sampling_params,
            )
            # append model turn with mask=1
            response_ids  += out.token_ids
            response_mask += [1] * len(out.token_ids)
            num_turns     += 1

            # parse schema v1.1 markup from the just-emitted text
            text = self.tokenizer.decode(out.token_ids)
            if FINAL_RE.search(text):
                break
            # env.step consumes the route block, returns obs string (+ env reward bookkeeping)
            step = env.step(text)
            if step.done:
                break
            obs_ids = self.tokenizer(step.obs, add_special_tokens=False).input_ids
            response_ids  += obs_ids
            response_mask += [0] * len(obs_ids)     # obs tokens excluded from policy loss

        return AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.rollout_config.response_length],
            response_mask=response_mask[: self.rollout_config.response_length],
            num_turns=num_turns,
            metrics={},
            extra_fields={"env_terminal_reward": env.terminal_reward()},
        )
```

### 4.2 `scripts/rl/uno_reward.py`

```python
import torch
from verl.protocol import DataProto
from verl.workers.reward_manager.abstract import AbstractRewardManager
from verl.workers.reward_manager.registry import register

@register("uno")
class UnoRewardManager(AbstractRewardManager):
    def __init__(self, tokenizer, num_examine, compute_score=None,
                 reward_fn_key="data_source", **kwargs):
        self.tokenizer, self.num_examine = tokenizer, num_examine

    def __call__(self, data: DataProto, return_dict=False):
        # Env already composed R = (1-α)R_outcome + α R_cost at the terminal step
        # and stored it in extra_fields["env_terminal_reward"] during rollout.
        reward_tensor = torch.zeros_like(data.batch["response_mask"], dtype=torch.float32)
        for i in range(len(data)):
            r = float(data.non_tensor_batch["extra_fields"][i]["env_terminal_reward"])
            # place on the last real token of each trajectory
            last = int(data.batch["response_mask"][i].sum().item()) - 1
            reward_tensor[i, last] = r
        if return_dict:
            return {"reward_tensor": reward_tensor, "reward_extra_info": {}}
        return reward_tensor
```

### 4.3 `scripts/rl/train_grpo.py`

```python
# Entry point — no monkey-patches. The @register imports are the
# registration: importing the module side-effects the registry.
import scripts.rl.uno_rollout   # noqa: F401 (registers "uno")
import scripts.rl.uno_reward    # noqa: F401 (registers "uno")

from verl.trainer.main_ppo import main

if __name__ == "__main__":
    main()
```

### 4.4 `scripts/rl/run_grpo.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
python scripts/rl/train_grpo.py \
    actor_rollout_ref.rollout.agent.agent_name=uno \
    reward_model.reward_manager=uno \
    actor_rollout_ref.rollout.multi_turn.max_turns=5 \
    algorithm.adv_estimator=grpo \
    algorithm.group_size=8 \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    data.train_files=data/rl/train.parquet \
    data.val_files=data/rl/val.parquet \
    actor_rollout_ref.model.path=/data/xieht/checkpoints/Uno-Orchestra-7B-SFT \
    trainer.n_gpus_per_node=4 trainer.nnodes=1 \
    trainer.total_epochs=1
```

### 4.5 `tests/rl/test_rollout_byte_identity.py`

```python
import json, torch
from unittest.mock import AsyncMock, MagicMock
from scripts.rl.uno_rollout import UnoAgentLoop

def test_rollout_byte_identity():
    fx = json.load(open("tests/rl/fixtures/sft_golden_trajectory.json"))

    # stub server_manager.generate to replay fx["policy_turns"]
    server = MagicMock()
    turns = list(fx["policy_turn_token_ids"])
    async def fake_generate(**_): return MagicMock(token_ids=turns.pop(0), log_probs=None)
    server.generate = AsyncMock(side_effect=fake_generate)

    # build loop with real tokenizer + mocked server + canned env
    loop = UnoAgentLoop(
        trainer_config=..., server_manager=server, tokenizer=...,
        processor=None, dataset_cls=..., data_config=...,
    )
    out = loop.loop.run_until_complete(
        loop.run(sampling_params={}, raw_prompt=fx["raw_prompt"], extra_info=fx["extra_info"])
    )
    assert out.response_ids == fx["expected_response_ids"]
    assert out.response_mask == fx["expected_response_mask"]
```

### 4.6 Per-step call order under verl 0.7

```
verl.trainer.main_ppo.main()
  └─ RayPPOTrainer.fit()
       └─ for batch in dataloader:                    # DataProto batches
            ├─ rollout = AgentLoopManager.run(batch)   # spawns AgentLoopWorkers
            │     └─ for each sample → UnoAgentLoop.run(**row)
            │          ├─ apply_chat_template(raw_prompt) → prompt_ids
            │          ├─ for turn in range(max_turns):
            │          │    ├─ server_manager.generate(prompt_ids + response_ids)
            │          │    └─ env.step(text) → obs / done
            │          └─ AgentLoopOutput(prompt_ids, response_ids, response_mask, extra_fields)
            ├─ reward  = UnoRewardManager(rollout_out)  # from REWARD_MANAGER_REGISTRY
            ├─ advantages = grpo_group_norm(...)
            ├─ actor.update(rollout_out, advantages)
            └─ (optional) ref.kl_penalty
```

---

## 5. Risks / Open Questions Surfaced

1. **vllm upgrade is not free.** Current env has vllm 0.6.3; upstream
   v0.7 refuses anything below 0.7.0. We must upgrade to 0.11.x and
   re-test sleep/wake on the 4-GPU config. Sleep-level becomes 2
   (supported by 0.8.5+). No code changes on our side — this is a
   `pip install` + re-verify.
2. **Switching to pip-installed verl v0.7 invalidates the cached
   Gate-2 PASS on our shim.** The Gate-2 run was against the
   vendored tree; deleting that tree means we start integration
   testing fresh under upstream. Shouldn't regress (upstream is
   strictly more capable), but the smoke (Step 6) is the gate.
3. **SFT byte-identity fixture still needs to be built** (Step 1).
   The fixture must use the exact tokenizer + chat template the SFT
   was trained with. Need to cross-check
   `/data/xieht/checkpoints/Uno-Orchestra-7B-SFT/tokenizer_config.json`
   against what `AgentLoopBase.apply_chat_template()` would produce.
4. **Upstream is pre-1.0 and fast-moving.** Main HEAD is already
   `0.8.0.dev`. We should pin to tag `v0.7.0` (`f9c855f`) for the
   paper run — otherwise API drift between survey and smoke is real.

---

## 6. Gate Checklist

- [x] All 3 sign-off questions answered with file:line evidence.
- [x] Compat matrix has no 🟡 rows — all 🟢 Go under upstream v0.7.
- [x] Delete/Keep list is evidence-backed (git tag diff + file
      inspection; not preference).
- [x] Pseudocode skeletons match the compat-matrix contract
      (imports and base classes are the real v0.7 API).
- [x] Aggregate verdict stated: **Go**, contingent on structural
      decision in §3.

**User sign-off required for §3.1 / §3.2** (delete vendored
`verl/` + `vllm_v_0_11_0/` shim, switch to pip-installed upstream
verl v0.7.0). This is a bigger delete than the plan originally
implied — flagging explicitly before executing.
