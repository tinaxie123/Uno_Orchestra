# SkillRouter: Hierarchical Decomposition and Skill Routing via Multi-Agent Reinforcement Learning

## 1. 问题背景

当前多智能体系统（Multi-Agent Systems）在复杂任务求解中被广泛应用于深度代码工程（Codex, Devin）、广度信息检索（Kimi, Perplexity）、复杂推理（多Agent辩论）等场景。然而，现有系统面临两个核心问题：

- **Prompt-based orchestration（AOrchestra）**：依赖大模型prompt做调度，成本高、不稳定、无法从错误中学习。
- **固定routing的RL系统（WideSeek-R1）**：通过MARL学会了任务拆分，但所有sub-agent同质（都做搜索），缺乏技能维度的路由能力。
- **模型路由系统（Router-R1, xRouter, Atlas）**：通过RL学会了选择哪个模型，但不拆分任务，无法处理需要多种异质技能协作的复杂query。

**Gap：没有系统能同时通过RL学习"如何拆分任务"和"每个子任务该用什么技能"。**

## 2. 研究问题

> 能否通过强化学习，让一个小模型同时学会（1）将复杂query拆分为子任务，以及（2）为每个子任务从异质技能池中分配最合适的技能——且无需显式的拆分监督信号？

## 3. 方法：SkillRouter

### 3.1 架构

```
Query: "查2024诺贝尔物理奖得主并画出10年论文趋势图"
  │
  ▼
┌─────────────────────────────────────┐
│ Conductor / 主智能体 (Qwen3-4B)     │ ← RL训练
│ 职责：任务拆分 + 技能分配            │
│                                     │
│ <think>需要搜索+写代码，拆开</think> │
│ <plan>                              │
│   sub1: 查诺贝尔奖得主 | web_search │
│   sub2: 查论文数量     | web_search │
│   sub3: 画折线图       | code_exec  │
│ </plan>                             │
└──────────┬──────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│ Sub-agents / 执行智能体 (Qwen3-8B)  │ ← 不训练
│ 每个sub-agent = 现成模型 + skill prompt + tool权限  │
│                                     │
│ sub-agent 1: web_search专家 → 执行sub1  │
│ sub-agent 2: web_search专家 → 执行sub2  │
│ sub-agent 3: code_exec专家  → 执行sub3  │
└──────────┬──────────────────────────┘
           │
           ▼
       汇总 → 最终答案 → Judge评分 → Reward
```

**核心设计：**

- **训练的**：Conductor（小模型3-4B），学习拆分+路由决策
- **不训练的**：Sub-agents（现成大模型），接收skill prompt执行任务
- **Skill Pool**：预定义的异质技能集（web\_search, code\_exec, calculator, summarizer, direct\_solve等），每个skill = system prompt + tool API权限

### 3.2 训练算法：HCPO-GSPO

我们将HCPO（Hierarchical Conductor-Based Policy Optimization）的理论框架与GSPO（Group Sequence Policy Optimization）结合，提出适用于LLM场景的联合优化算法。

**Advantage分解（基于HCPO Lemma 2）：**

```
A_total(s, M, a) = A_conductor(M|s) + A_agent(a|s, M)

A_conductor = 拆分方案的advantage（"这样拆好不好"）
A_agent     = 技能选择的advantage（"给定拆分后，选这个skill对不对"）
```

**更新规则：**

```
Step 1: 更新Critic — V(query) ← 拟合实际reward
Step 2: 更新Agent策略（skill选择）— GSPO序列级优化
Step 3: 更新Conductor策略（任务拆分）— GSPO序列级优化
```

**理论保证（我们的Proposition 1）：**

GSPO的序列级clip蕴含KL上界：$D\_{KL}(\pi\_{new} | \pi\_{old}) \leq \frac{\epsilon^2}{1-\epsilon}$

结合HCPO的KL分解性质：$D\_{KL}(\text{联合策略}) \leq D\_{KL}(\text{conductor}) + \sum D\_{KL}(\text{agent}\_i)$

推得：在GSPO更新下，联合策略的单调改进保证仍然成立。即每次更新后，系统整体性能保证不变差。

**Reward设计：**

```
R = correctness - λ · token_cost

correctness: success-gated（答对=1, 答错=0, 借鉴xRouter）
token_cost:  normalized(conductor生成 + sub-agent执行的总token开销)
```

### 3.3 核心Insight

**拆分质量和路由质量是深度耦合的：**

- 拆得差（"查诺贝尔奖得主并用Python画图"）→ 杂糅多个skill → Router无法分配单一skill → 执行失败 → reward=0
- 拆得好（"查得主" / "查论文数" / "画图"）→ 每个sub-task只需一个skill → Router分配准确 → 执行成功 → reward高

**Single-skill decomposition作为emergent behavior自然涌现：** RL训练过程中，模型自动学会"把multi-skill任务拆成single-skill子任务"，无需显式监督。

## 4. 与现有工作的定位

| 系统                     |  任务拆分  |      技能路由      | 联合RL训练 |  并行执行 |        理论保证       |
| ---------------------- | :----: | :------------: | :----: | :---: | :---------------: |
| AOrchestra             | Prompt |     Prompt     |    ❌   |   ❌   |         ❌         |
| Puppeteer (NeurIPS'25) |    ❌   |   RL(选agent)   |    ❌   | ❌(串行) |         ❌         |
| WideSeek-R1            |   RL   |      ❌(同质)     |   部分   |   ✅   |         ❌         |
| Router-R1              |    ❌   |     RL(选模型)    |    ❌   |   ❌   |         ❌         |
| xRouter                |    ❌   |     RL(选模型)    |    ❌   |   ❌   |         ❌         |
| Atlas                  |    ❌   |   Cluster+RL   |    ❌   |   ❌   |         ❌         |
| **SkillRouter (Ours)** | **RL** | **RL(选skill)** |  **✅** | **✅** | **✅ (HCPO-GSPO)** |

## 5. 实验计划

### 训练数据

多领域混合，只需query + ground\_truth answer（\~12K条）：

| 领域      | 数据集             | 数量 |
| ------- | --------------- | -- |
| QA      | NQ, HotpotQA    | 5K |
| Math    | GSM8K, MATH     | 3K |
| Code    | MBPP, HumanEval | 2K |
| Agentic | GAIA, FRAMES    | 2K |

### 评测

**In-Distribution：** HotpotQA, GSM8K, MBPP (训练时见过的领域)
**Out-of-Distribution：** GAIA, FRAMES, DROP, AIME24 (训练时未见过的领域)

### Baselines

AOrchestra, WideSeek-R1, Puppeteer, Router-R1, xRouter, Atlas, Direct Prompting

### 关键分析

- Pareto frontier：accuracy vs cost（所有系统对比）
- Emergent skill specialization：不同领域query的skill使用分布热力图
- Cross-domain transfer：在未见领域的routing泛化能力
- Ablation：拆分-only / 路由-only / SFT-only vs RL / Router大小 / Skill pool大小 / GRPO vs GSPO vs HCPO-GSPO

## 6. 预期贡献

1. **框架**：首个通过RL联合学习任务拆分与异质技能路由的multi-agent系统。
2. **算法**：HCPO-GSPO——将hierarchical conductor-agent优化与序列级策略优化结合，提供联合训练的单调改进理论保证。
3. **Emergent Behavior**：证明single-skill decomposition从correctness+cost reward中自然涌现，无需显式监督。
4. **效率**：3-4B trained router达到或超越GPT-4级prompt-based orchestration的效果，routing成本降低10-50×。
5. **泛化**：多领域混合训练实现跨域zero-shot routing。

## 7. 目标

NeurIPS 2026
