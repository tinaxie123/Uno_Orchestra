# SkillRouter: Learning to Decompose and Route via Reinforcement Learning

## Core Claim

A single small model (3-4B) can learn to **decompose** complex queries into single-skill subtasks and **route** each subtask to the appropriate skill, achieving better performance than prompt-based orchestration (AOrchestra) and fixed-routing systems (WideSeek-R1), while being 10-50× cheaper than large-model routing (xRouter).

**Key insight: Good decomposition = each subtask falls into exactly one skill's capability. The model learns this through RL without explicit decomposition supervision.**

---

## One-Line Contribution

First framework to jointly learn task decomposition and skill routing via RL, where a single trained model outputs both the decomposition plan and skill assignments in one autoregressive generation.

---

## Architecture

```
Query: "查2024诺贝尔物理奖得主并画10年论文趋势图"
  │
  ▼
┌─────────────────────────────────────────┐
│  SkillRouter (Qwen3-4B, trained)        │
│                                         │
│  <think>需要搜索+写代码，拆开</think>    │
│  <plan>                                 │
│    sub1: 查2024诺贝尔物理奖得主          │
│          | skill: web_search            │
│    sub2: 查{name}过去10年论文数量        │
│          | skill: web_search            │
│    sub3: 画折线图 {data}                │
│          | skill: code_exec             │
│  </plan>                                │
└─────────┬───────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────┐
│  Sub-agents (现成模型, 不训练)            │
│                                         │
│  sub-agent 1: Qwen3-8B + web_search     │
│    system: "你是搜索专家，使用search工具" │
│    → 执行sub1 → result_1                │
│                                         │
│  sub-agent 2: Qwen3-8B + web_search     │
│    → 执行sub2 → result_2                │
│                                         │
│  sub-agent 3: Qwen3-8B + code_exec      │
│    system: "你是Python程序员"            │
│    → 执行sub3 → result_3                │
└─────────┬───────────────────────────────┘
          │
          ▼
      汇总 → 最终答案
```

### 核心设计决策

- **训练的**: SkillRouter (单个3-4B模型，一次生成拆分+路由)
- **不训练的**: Sub-agents (现成instruct模型 + skill prompt + tool权限)
- **Skill = system prompt + tool权限**, 不是独立模型

---

## Skill Pool

```python
SKILL_POOL = {
    "direct_solve":  # 无工具，直接回答（简单知识题）
    "web_search":    # 搜索引擎检索（信息获取）
    "code_exec":     # Python sandbox执行（计算、画图、数据处理）
    "calculator":    # 数学计算（纯算术）
    "retriever":     # 从给定文档中检索（长文档QA）
    "summarizer":    # 长文本摘要
}
```

每个skill对应一个system prompt模板 + tool API权限。Sub-agent收到 (subtask + skill prompt) 执行并返回结果。

---

## Training

### Phase 0: 数据准备

从多领域QA数据集采样，只需要 query + ground_truth:

| 领域 | 数据集 | 数量 | 涉及skill |
|------|--------|------|-----------|
| QA | NQ, HotpotQA | 5K | web_search, direct_solve |
| Math | GSM8K, MATH | 3K | calculator, code_exec |
| Code | MBPP, HumanEval | 2K | code_exec |
| Agentic | GAIA, FRAMES | 2K | web_search, code_exec, retriever |
| **Total** | | **~12K** | |

### Phase 1: SFT Warm-start (可选)

用GPT-4/Claude对几千条query生成格式化示例:

```
输入: "法国和德国2023年GDP哪个高"
输出:
<think>需要查两个国家的GDP数据，拆成两个搜索</think>
<plan>
sub1: 查法国2023年GDP | skill: web_search
sub2: 查德国2023年GDP | skill: web_search
sub3: 对比{result_1}和{result_2} | skill: direct_solve
</plan>
```

- 只保留执行后答对的示例
- ~3000条够了，只教格式不教策略
- **也可以不做SFT，直接RL（Router-R1的做法）**

### Phase 2: RL (GSPO)

```python
for each batch of (query, ground_truth):

    # 1. Rollout: 模型生成拆分+路由
    output = skill_router.generate(query)
    subtasks, skills = parse_plan(output)

    # 2. 执行: sub-agents用分配的skill执行
    results = []
    total_cost = 0
    for st, sk in zip(subtasks, skills):
        result = sub_agent.execute(st, skill_prompt=SKILL_POOL[sk])
        results.append(result)
        total_cost += result.token_cost

    # 3. 汇总 + 判分
    final_answer = aggregate(results)
    correct = exact_match(final_answer, ground_truth)

    # 4. Reward (success-gated, 借鉴xRouter)
    if correct:
        reward = 1.0 - λ * normalize(total_cost)
    else:
        reward = 0.0

    # 5. GSPO更新 (序列级别，借鉴WideSeek-R1/Qwen3)
    gspo_update(skill_router, output, reward)
```

### RL算法选择: GSPO

| 算法 | 粒度 | 适合场景 | 用了它的系统 |
|------|------|----------|------------|
| PPO | token级 | 短序列 | Atlas |
| GRPO | token级 | 中等序列 | DeepSeek-R1, Router-R1 |
| **GSPO** | **序列级** | **长序列生成** | **WideSeek-R1, Qwen3** |
| DAPO | token级+分布式 | 大规模 | xRouter |

GSPO最适合：你的输出是长序列（think + plan），序列级clip更稳定。且Qwen3本身就是GSPO训的。

### Reward设计

```
R = correctness - λ · token_cost

correctness: success-gated (答对=1, 答错=0)
token_cost:  normalized(router生成 + sub-agent执行的总token)
λ:           cost-performance tradeoff系数，sweep [0.01, 0.05, 0.1, 0.3]
```

**不需要额外的independence reward / 信息瓶颈 / attention penalty。**
Token cost自然惩罚冗余拆分。Correctness自然奖励好的skill选择。

### 为什么好的拆分会自然涌现

```
坏的拆分（杂糅skill）:
  sub1: "查诺贝尔奖得主并用Python画图" | skill: code_exec
  → code_exec不能搜索 → 执行失败 → correctness=0 → R=0

好的拆分（single-skill）:
  sub1: "查得主" | web_search → 成功
  sub2: "查论文数" | web_search → 成功
  sub3: "画图" | code_exec → 成功
  → correctness=1, cost适中 → R>0 → 被强化
```

RL自然学到："拆成每个subtask只需要一个skill" = 更高reward。

---

## Model Configuration

| 角色 | 模型 | 大小 | 训练? |
|------|------|------|-------|
| SkillRouter | Qwen3-4B | 4B | ✅ SFT + GSPO |
| Sub-agent | Qwen3-8B / 32B | 8-32B | ❌ 现成模型 |
| Judge | GPT-4o / Qwen3-32B | - | ❌ API |

**小模型做决策，大模型做执行。**

---

## Evaluation

### In-Distribution (训练时见过的领域)

| Benchmark | 领域 | N | Split |
|-----------|------|---|-------|
| HotpotQA | Multi-hop QA | 500 | test |
| GSM8K | Math | 500 | test |
| MBPP | Code | 500 | test |

### Out-of-Distribution (训练时没见过的领域)

| Benchmark | 领域 | N | 为什么OOD |
|-----------|------|---|-----------|
| GAIA | Agentic | 165 | 复杂工具使用 |
| FRAMES | Multi-hop + retrieval | 500 | 新领域 |
| DROP | Reading comprehension | 500 | 需要数值推理 |
| AIME24 | Competition math | 30 | 难度远超训练集 |

### Baselines

| System | 类型 | Routing方式 |
|--------|------|------------|
| Direct (Qwen3-8B) | 单模型 | 无routing |
| AOrchestra | Prompt-based | GPT-4做routing (prompt) |
| WideSeek-R1 | RL multi-agent | 固定routing (全是search) |
| Router-R1 | RL router | 选模型不选skill |
| Atlas | Cluster + RL | 选model-tool pair |
| xRouter | RL router | 选模型，cost-aware |
| **SkillRouter (ours)** | **RL decompose+route** | **拆分+选skill，联合训练** |

### Metrics

- Accuracy (EM / F1)
- Total cost (tokens consumed)
- Routing cost (router tokens only)
- Pareto curve (accuracy vs cost)

---

## Differentiation

| | AOrchestra | WideSeek-R1 | Router-R1 | xRouter | Atlas | **Ours** |
|---|---|---|---|---|---|---|
| 拆分 | prompt | RL训练 | ❌ | ❌ | ❌ | **RL训练** |
| 路由 | prompt | ❌(同质) | RL(选模型) | RL(选模型) | RL(选model-tool) | **RL(选skill)** |
| 联合训练拆分+路由 | ❌ | ❌ | ❌ | ❌ | ❌ | **✅** |
| Skill pool | ❌ | ❌ | ❌ | ❌ | 有 | **有** |
| 多领域泛化 | ❌ | ❌ | ❌ | ❌ | ✅ | **✅** |
| Router大小 | GPT-4 | 4B | 3B | 7B | 3B | **3-4B** |

**核心novelty: 第一个用RL联合训练task decomposition + skill routing的框架。**

---

## Tables and Figures

| # | 内容 | 作用 |
|---|------|------|
| **Table 1** | ID evaluation: 各系统在HotpotQA/GSM8K/MBPP上的accuracy+cost | 主实验 |
| **Table 2** | OOD evaluation: GAIA/FRAMES/DROP/AIME24 | 泛化能力 |
| **Table 3** | Ablation: SFT-only vs RL, 不同skill pool大小, 不同router大小 | 方法分析 |
| **Fig 1** | 架构图 | 方法展示 |
| **Fig 2** | Pareto curve: accuracy vs cost (所有系统) | 核心结果 |
| **Fig 3** | Skill使用分布: 不同领域query的skill选择热力图 | 展示router学到了"场景" |
| **Fig 4** | 拆分质量分析: router学到的拆分方案 vs prompt-based拆分 | 展示decomposition质量 |
| **Fig 5** | OOD transfer: 训练领域 vs 测试领域的性能矩阵 | 泛化能力 |

### 关键Ablation

| 实验 | 问题 |
|------|------|
| 不拆分，只路由（类似Router-R1） | 拆分有没有用？ |
| 不路由，只拆分（全给同一个skill） | 路由有没有用？ |
| SFT-only vs SFT+RL | RL有没有必要？ |
| 3B vs 4B vs 8B router | 多大的router够？ |
| Skill pool 3个 vs 6个 vs 10个 | Pool大小影响？ |
| GRPO vs GSPO vs PPO | 哪个RL算法好？ |
| 有SFT warm-start vs 纯RL | SFT有没有必要？ |

---

## Infrastructure: 4-8×A800 80GB

```
GPU 0-1: Sub-agent vLLM serving (Qwen3-8B, 用于执行sub-tasks)
GPU 2:   Judge (Qwen3-32B 或 API)
GPU 3:   SkillRouter训练 (Qwen3-4B, GSPO)

或者全用API执行sub-tasks，GPU全用于router训练
```

---

## Execution Plan

### Week 1: 数据准备 + Pipeline搭建

```
- 采样训练数据 (NQ, HotpotQA, GSM8K, MBPP, ~12K)
- 实现skill pool (system prompt + tool API)
- 实现sub-agent执行pipeline
- 实现reward计算 (correctness + cost)
- 端到端验证: query → router → sub-agents → judge → reward
```

### Week 2: SFT + RL训练

```
- (可选) 用GPT-4生成SFT数据 (~3K条)
- SFT warm-start Qwen3-4B
- 实现GSPO训练loop
- 开始RL训练, sweep λ
- 监控reward曲线, 调参
```

### Week 3: 评测 + Baselines

```
- 在ID benchmarks上评测
- 在OOD benchmarks上评测
- 跑所有baselines (AOrchestra, WideSeek-R1, Router-R1, xRouter)
- 生成Tables 1-3, Figures 2-5
```

### Week 4: Ablation + Paper

```
- 跑所有ablation实验
- 分析skill使用分布, 拆分质量
- 写paper
```

---

## Risk and Contingency

| 风险 | 后果 | 对策 |
|------|------|------|
| RL不收敛 | 没有trained router | 用SFT-only作为fallback (仍有contribution) |
| 打不过AOrchestra | GPT-4 routing太强 | Focus on cost efficiency: "同等效果，1/50成本" |
| OOD泛化差 | 只在训练领域work | 增加训练领域多样性，或focus on ID结果 |
| Sub-agent执行不稳定 | Reward noisy | 增加rollout次数(K=3-5), 用多seed平均 |
| Skill pool太小/太大 | Router学不好 | 做pool size ablation, 找sweet spot |

---

## Paper Story

> **问题**: 现有multi-agent系统要么用prompt做routing（贵且不稳定），要么用RL只学模型选择（不拆分任务）。没有人用RL联合学习"怎么拆"和"拆完给谁"。
>
> **方法**: SkillRouter — 一个3-4B模型，通过GSPO学会在一次生成中输出task decomposition和skill routing。Sub-agents用现成大模型+skill prompt执行。
>
> **发现**:
> 1. RL训练后，router自然学会把multi-skill query拆成single-skill subtasks
> 2. 在多领域benchmark上match/beat大模型prompt-based orchestration
> 3. Routing成本比xRouter/AOrchestra低一个数量级
> 4. Cross-domain transfer: 在没见过的领域也能有效routing
