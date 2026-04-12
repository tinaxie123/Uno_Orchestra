# SkillRouter 实验计划：NeurIPS 2026 投稿准备

## 一、当前现状

### 已完成
- SFT 模型：Qwen2.5-7B 在 58K 条数据上做了 full SFT（`router_qwen25_7b_full_sft`）
- RL 训练：GiGPO，step 24/100 进行中，4x H100，预计还需 ~8h
- 当前 RL success_rate：25.8%（从 step 1 的 9.4% 上升）

### 核心问题
1. **RL vs SFT 归因不清**：SFT 和 RL 使用完全相同的 system prompt（含完整 schema v1.1 规范、12 条 hard rules、3 个 exemplars），无法区分格式遵循能力来自 SFT 还是 RL
2. **缺少 baseline 对比**：没有 SFT-only、random routing、fixed-best-model 的对比数据
3. **数学类任务 success_rate ≈ 0%**，说明 routing 策略对这类任务无效
4. **没有 cost-quality tradeoff 分析**——这本应是 routing 的核心卖点

---

## 二、必须补的 Baseline 实验（优先级从高到低）

### Experiment 1: SFT-only Baseline（最关键）
**目的**：RL 到底比 SFT 好多少？

**方案**：
- 直接用 `router_qwen25_7b_full_sft` 模型（不经过 RL）在 rl_val_v3 上 evaluate
- 用与 RL 相同的 env（`SingleSkillRouterEnv`），相同的 system prompt
- temperature=0（greedy）和 temperature=0.7 各跑一次
- 记录：success_rate、reward/mean、cost、各子任务成功率、valid_action_ratio

**预期**：
- 如果 SFT-only 已经 >20% success_rate → RL 的贡献很小，story 需要大改
- 如果 SFT-only <10% → RL 确实有显著提升，可以继续当前方向

**实现**：写一个 eval 脚本，加载 SFT 模型，在 val set 上跑 multi-step rollout，调用相同的环境

### Experiment 2: Oracle Baseline（Upper Bound）
**目的**：全用最强模型能到多少？

**方案**：
- 所有 route 固定使用 `claude-opus-4-6`（映射到 qwen-max, 512 tokens）
- 所有 skill 固定使用 `reason`
- 使用 one-shot 策略（单轮 plan → 单个 route → final_answer）
- 记录 success_rate 和 cost

**预期**：这个 upper bound 定义了 routing 的天花板

### Experiment 3: Cheapest-Only Baseline（Lower Bound）
**目的**：全用最便宜模型效果如何？

**方案**：
- 所有 route 固定使用 `claude-haiku-4-5-20251001`（映射到 qwen-plus, 64 tokens）
- skill 固定 `direct_answer`
- 记录 success_rate 和 cost

### Experiment 4: Random Routing Baseline
**目的**：随机选 model/skill 的效果

**方案**：
- 使用 SFT 模型生成 plan/subtask 结构
- 但 route 中的 model 和 skill 随机从 valid pool 中采样
- 跑 5 次取平均

### Experiment 5: Prompt Ablation（证明 RL 内化了能力）
**目的**：回答 "去掉详细 prompt，RL 模型还能用吗？"

**方案 A - Minimal Prompt**：
- 只给："You are a task router. Decompose the question and route subtasks to appropriate models."
- 不给 schema 规范、hard rules、exemplars
- 分别在 SFT 模型和 RL 模型上测试

**方案 B - No Exemplars**：
- 保留 schema 和 rules，但去掉 3 个 exemplars
- 分别测试 SFT 和 RL

**预期**：
- 如果 RL 模型在 minimal prompt 下仍保持格式 → RL 真的内化了格式能力
- 如果两者都崩 → 格式能力完全 prompt-driven，RL 在格式方面无贡献

---

## 三、Cost-Quality Pareto 分析（论文核心图表）

这是 routing 论文最重要的实验，必须有。

**方案**：
- X 轴：每个 episode 的平均 cost（用 MODEL_COST_PER_M_TOKENS 计算）
- Y 轴：success_rate
- 画出以下点：
  1. Cheapest-only（低 cost，低 accuracy）
  2. Random routing（中 cost，低 accuracy）
  3. SFT router（中 cost，? accuracy）
  4. RL router（中 cost，? accuracy）
  5. Oracle/all-opus（高 cost，高 accuracy）
- 如果 RL router 在 pareto 前沿 → 有实用价值
- 如果 RL router 和 SFT router 重叠 → RL 没用

**额外分析**：
- 按 domain 分类画（math / code / science / multihop_qa）
- 分析 RL 模型学到了什么路由偏好：哪些任务用便宜模型、哪些用贵模型

---

## 四、论文 Story 方向建议

### 当前 Story（弱）
> "我们用 RL 训了一个比 SFT 更好的 router"

问题：improvement 可能不显著，novelty 不够

### 建议 Story A：Cost-Aware Routing（实用导向）
> "RL 训练的 router 能在降低 X% cost 的同时保持 Y% 的 accuracy，优于 SFT router 和 rule-based baselines"

关键指标：Pareto efficiency，cost reduction ratio
适合：NeurIPS industry track 或 workshop

### 建议 Story B：RL for Compositional Decision Making（方法导向）
> "Multi-step task decomposition + model routing 是一个 compositional decision problem，SFT 只能模仿 teacher 的决策，而 RL 能通过 exploration 发现更优的路由策略"

需要证明：
- RL 发现了 SFT 数据中不存在的路由模式
- 这些新模式确实更优
- 泛化到 OOD 任务

适合：NeurIPS main conference（如果证据充分）

### 建议 Story C：Reward Design for Agentic Systems（分析导向）
> "我们研究了不同 reward 设计（format reward, outcome reward, cost reward）对 agentic RL 训练的影响"

需要：多组 reward ablation 实验
适合：analysis paper，NeurIPS poster

---

## 五、执行优先级

| 优先级 | 实验 | 预计耗时 | 依赖 |
|--------|------|----------|------|
| P0 | Exp 1: SFT-only eval | 2-3h（写脚本+推理） | 无 |
| P0 | 等当前 RL 训练完成 | ~8h | 无 |
| P1 | Exp 2+3: Oracle + Cheapest | 1-2h | 写 eval 脚本 |
| P1 | Exp 4: Random routing | 2h | 同上 |
| P1 | Cost-Quality Pareto 图 | 1h | Exp 1-4 完成 |
| P2 | Exp 5: Prompt ablation | 3-4h | RL 训练完成 |
| P2 | RL routing pattern 分析 | 2h | RL 训练完成 |

**建议：现在立刻开始 Exp 1（SFT-only eval），不用等 RL 训练完。这个结果决定整个论文方向。**

---

## 六、关键数据路径

- SFT 模型：`/home/xieht/data/sft/checkpoints/router_qwen25_7b_full_sft`
- RL checkpoint（训练中）：`/home/xieht/data/sft/checkpoints/rl_gigpo_final`
- RL 训练数据：`/home/xieht/data/sft/rl_train_v3.parquet`（7901 条，math+code+science）
- RL 验证数据：`/home/xieht/data/sft/rl_val_v3.parquet`（3422 条，drop+numinamath+leetcode）
- SFT 训练数据：`/home/xieht/data/sft/train_final.parquet`（58457 条）
- 环境实现：`/home/xieht/data/verl-agent/agent_system/environments/env_package/skillrouter/envs.py`
- RL 训练日志：`/home/xieht/data/sft/rl_train_format_reward.log`

## 七、环境关键细节（写论文/设计实验时注意）

- **所有 model 都映射到 DashScope API**：nano tier → qwen-plus(64 tokens), mid tier → qwen-plus(256), large tier → qwen-max(512), code → qwen-plus(512)
- **cost 按虚拟定价计算**，不是实际 API cost（模拟真实 model 的价格差异）
- **reward = (1-α)*R_outcome + α*R_cost**，当前 α=0.1
- **val set 和 train set 的 domain 不重叠**：train 有 math_dapo/mega-science/code-taco，val 有 drop/numinamath_cn_k12/leetcode-Medium → 这本身就是在测 OOD 泛化
