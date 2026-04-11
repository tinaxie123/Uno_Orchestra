# Experiment Guide (Explained)

这是一份“解释版”实验文档。  
它不替代任何已经锁定的规范文件，而是帮助快速理解：

- 这篇工作的核心问题是什么
- 当前系统架构是什么
- 为什么 schema / benchmark / recipe 要这样设计
- 代码和实验接下来应该按什么顺序推进

如果你要看**硬约束**，请以以下文件为准：
- `data/trajectory_schema.md`
- `experiment_plan_v2.md`
- `config/pools.yaml`
- `config/sft_recipe.yaml`
- `scripts/validate_schema.py`

---

## 1. 这篇工作到底在解决什么问题

这篇工作的核心不是“再做一个复杂多智能体系统”，而是研究：

> **什么时候需要 delegation，什么时候不需要。**

更具体一点：
- 有些任务需要拆解成多个子任务，再分别调用不同能力去处理
- 但很多任务其实不需要 persistent multi-agent collaboration
- 这些任务更适合退化成：
  - `direct_solve`
  - `retrieval`
  - `code_exec`
  - 其他 stateless skill

所以这篇工作的主张是：

> **多代理系统的收益，很多时候来自“按需拆解 + 正确路由”，而不是来自 memoryful worker collaboration 本身。**

---

## 2. 当前系统的最终 formulation

当前已经敲定的方法是：

## Single-Shot Decomposition with Iterative Repair

流程如下：

1. **Leader** 一次性输出一个 subtask set
2. **Router** 为每个 subtask 选择 `(model, skill)`
3. **Executor** 并行执行所有可执行子任务
4. **Verifier** 判断当前结果是否足够
5. 如果不够，则进入下一轮 **repair**
6. 如果足够，则输出 `<final_answer>`

这意味着：
- 主范式是 **并行单拍**
- 容错机制是 **顺序补拍**

所以它既不是：
- 纯 sequential ReAct

也不是：
- 纯固定 planner-worker 多代理

而是：
- **single-shot planning**
- **parallel execution**
- **iterative repair**

---

## 3. 为什么不是传统多代理

这个系统和传统 MAS 的关键区别在于：

- subtask 不一定对应“一个会长期对话的 agent”
- 很多 subtask 只是一次性 skill invocation
- route 的核心对象不是“哪个 worker”
- 而是：
  - 哪个 model
  - 哪个 skill
  - 是否直接解

因此它的研究重心是：

> **capability routing**

而不是：

> **agent orchestration**

这也是它和 WideSeek / AOrchestra / Atlas 真正拉开边界的地方。

---

## 4. 为什么 schema 要设计成现在这样

当前 trajectory schema 的关键对象有：

- `<plan round="N">`
- `<subtask id="K" depends_on="...">`
- `<route round="N" subtask="K" model="..." skill="...">`
- `<obs subtask="K">`
- `<verify round="N" status="...">`
- `<final_answer>`

这套 schema 的作用不是“好看”，而是为下面三件事服务：

### 4.1 让 planning 结构显式化
- `<plan>` 表示一轮拆解
- `<subtask>` 明确任务图节点
- `depends_on` 明确 DAG 依赖关系

### 4.2 让 routing 成为独立的可学习动作
- `<route>` 明确指向：
  - 一个 subtask
  - 一个 model
  - 一个 skill

这就是 Router 的动作锚点。

### 4.3 让 repair 成为一等公民
- `<verify status="pass">`：直接结束
- `<verify status="repair_needed">`：进入下一轮 `<plan>`

没有这部分，模型很难学会“发现缺口再补一次”。

---

## 5. 为什么不把 `lazy` 当成一个 domain

这是最近一个很重要的澄清。

`lazy` 不是任务领域，而是**策略行为**。

所以现在推荐把数据划分成两层：

### 任务领域（domain）
- `multi_hop_qa`
- `single_hop_qa`
- `math`
- `code`
- `stem`
- `commonsense_social`
- `formal_logic`
- `long_context`
- `domain_knowledge`
- `tool_agent`

### 行为目标（behavior_target）
- `lazy`
- `decompose`
- `mixed`

例如：
- `single_hop_qa + lazy`
- `multi_hop_qa + decompose`
- `math + mixed`

这样任务内容和决策策略就分开了，文档、图表、recipe 都会更清楚。

---

## 6. 为什么 benchmark 必须 train/eval 分离

当前项目已经立下硬规则：

> **凡是打算进主表的 benchmark，一律不允许进入任何训练过程。**

包括：
- SFT
- RL
- distillation seed
- in-context examples

这样做的原因很简单：
- 防止 benchmark contamination
- 让论文主结论建立在 held-out generalization 上

典型例子：
- `GAIA`：只 eval，不进训练
- `BrowseComp-Plus`：只 eval，不进训练
- `ToolBench`：只 eval，不进训练
- `WideSearch`：只 eval，不进训练

训练集应该来自：
- 经典大规模非主 benchmark 数据
- 以及你自己的蒸馏轨迹

---

## 7. MMLU 和热门 benchmark 应该怎么处理

MMLU 这类热门 benchmark 当然可以作为 eval，但它们不适合承担这篇工作的主结论。

原因不是它们“没用”，而是它们和当前论文的核心机制不完全对齐：
- MMLU 主要测试广泛知识和单轮推理
- 但这篇工作主要研究：
  - 什么时候拆分
  - 拆分后如何 route
  - 什么时候 repair
  - 什么时候 collapse 到 `direct_solve`

因此，像 MMLU 这样的 benchmark 更适合：
- secondary eval
- appendix
- general capability sanity check

而不适合做 main table 的中心 benchmark。

### 7.1 推荐原则

- **主表 benchmark**：严格 held-out，不进入任何训练阶段
- **热门 benchmark**：如果与训练数据家族存在重叠，只作为 supporting evidence，不作为核心 claim 证据
- **最热门、最饱和的数据集**：例如 MMLU / MBPP / HumanEval，更适合作为 appendix sanity check

### 7.2 对 MMLU 的具体建议

- 如果训练中已经使用了 `MMLU auxiliary_train` 一类数据：
  - 不再把 `MMLU` 当成干净 held-out 主 benchmark
  - 最多作为 appendix / broad capability check

- 如果未来完全移除 MMLU family 训练数据：
  - 可以重新启用 `MMLU` 作为 secondary eval
  - 但仍然不建议让它承载主结论

### 7.3 当前建议定位

- `GAIA` / `BrowseComp-Plus` / `WideSearch` / `ToolBench` / `Terminal-Bench 2.0`
  - 用于证明方法主张
- `MMLU` / `GPQA` / `LongBench v2`
  - 用于补充泛化与 broad capability 画像
- `MBPP` / `HumanEval`
  - 不建议主打，只做简单 sanity check

---

## 8. 当前 SFT 数据为什么这样配

当前 Phase 1 配方的目标不是“什么都学”，而是先覆盖三类能力：

### 7.1 学会什么时候不拆
来自：
- `NQ Open`
- `TriviaQA`
- `WebQuestions`
- `CommonsenseQA`
- `PIQA`

### 7.2 学会什么时候拆
来自：
- `HotpotQA`
- `2WikiMultihopQA`
- `MuSiQue`
- `StrategyQA`
- `Qasper`

### 7.3 学会拆完怎么 route
来自：
- `MATH`
- `CodeContests`
- `APPS`
- `FinQA`
- `API-Bank`
- `ToolACE`

这就是为什么 recipe 不是乱拼的，而是围绕：
- no decomposition
- decomposition
- capability routing

三个维度搭出来的。

---

## 9. 为什么不直接全量蒸馏

虽然 schema、recipe、benchmark split 都已经接近锁定，但还不能直接上 4 万多条全量蒸馏。

原因有四个：

### 8.1 数据源未必都能稳定加载
有些 HF 数据集会有：
- split 不存在
- 字段名和预期不一致
- 镜像不可用

所以必须先跑 `availability_probe.py`。

### 8.2 teacher 未必稳定遵守 schema
即使 prompt 写得很好，teacher 也可能：
- 漏 `<verify>`
- round 搞乱
- `depends_on` 非法
- `repair_needed` 后直接 `final_answer`

所以必须先做 dry run。

### 8.3 开放的 model × skill pairing 会带来脏探索
当前策略倾向于：
- `model` 闭集
- `skill` 闭集
- pairing 开放

这在研究上是合理的，但前期必须看清：
- 哪些 pair 频繁出现
- 哪些 pair 明显不经济

### 8.4 成本不低
第一阶段蒸馏预算已经接近：
- `$1800–$2200`

没做 dry run 就全量开跑，风险太高。

---

## 10. 当前代码推进顺序为什么这样排

当前最合理的顺序是：

1. 锁 schema
2. 锁 experiment plan
3. 锁 benchmark split
4. 写 `validate_schema.py`
5. 写 `pools.yaml`
6. 写 `sft_recipe.yaml`
7. 写 `availability_probe.py`
8. 写 `generate_trajectories.py`
9. 跑 30 条 dry run
10. 跑 300–500 条 pilot
11. 再开全量蒸馏
12. 然后才进入 SFT / RL

这个顺序的逻辑是：

- 先把协议和约束锁死
- 再写入口检查器
- 再写生成器
- 最后才花 API 钱和 GPU 钱

这样代价最低。

---

## 11. 当前最重要的几个文件分别干嘛

### `data/trajectory_schema.md`
定义 trajectory 格式：
- 什么 tag 合法
- 什么顺序合法
- `verify` / `repair` 状态机是什么

### `experiment_plan_v2.md`
定义实验硬规则：
- 哪些 benchmark 只 eval
- 哪些数据进训练
- 当前 Phase 1 / Phase 2 计划

### `config/pools.yaml`
定义 routing action vocabulary：
- 允许使用哪些 model
- 允许使用哪些 skill

### `config/sft_recipe.yaml`
定义 Phase 1 训练配方：
- 30 个 dataset
- 41.5k 样本
- 每个数据集的 teacher / 抽样规模 / 备注

### `scripts/validate_schema.py`
定义“合格轨迹”的检查器：
- 规则验证
- 错误码
- 统计信息

### `scripts/availability_probe.py`
定义“合格数据源”的检查器：
- 能不能下载
- split 存不存在
- 样本字段对不对

---

## 12. 当前真正的风险点

现在最大的风险已经不在“想法对不对”，而在：

- availability probe 后会不会发现 recipe 要换数据集
- teacher 是否能稳定产出 repair 样本
- 路由分布会不会塌缩到极少数 model/skill
- `direct_solve` 会不会过强或过弱
- verify 是否能学会“该修就修”

这些都必须靠 dry run 暴露。

---

## 13. 现在如果只记一件事

当前项目最核心的一句话可以概括成：

> **先把“什么时候拆、怎么 route、什么时候 repair”学出来，再谈大规模训练。**

所以后续所有实现都应该围绕这条主线来判断：

- 这个改动是帮助学习结构化决策，还是在增加工程噪音？

如果是前者，就值得做。  
如果是后者，就应该延后。

---

## 14. 当前建议的实际下一步

如果只是按最稳妥的执行链路往前走，当前建议就是：

1. 跑 `availability_probe.py`
2. 修掉 probe 中暴露的数据源问题
3. 完成 `generate_trajectories.py`
4. 做 30 条 dry run
5. 人工 review
6. 再决定是否放量

不要跳过 dry run。
