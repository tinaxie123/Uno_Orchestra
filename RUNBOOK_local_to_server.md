# Local-to-Server Runbook

本文件是项目的执行手册，目标是把“本地写代码、远程跑实验”的流程固定下来，避免每次上线或开跑前反复确认。

适用场景：
- 本地开发：Mac / Trae / VS Code
- 远程训练或实验：Linux 服务器
- 代码同步方式：优先 `git push` / `git pull`
- 临时快照方式：`tar.gz + scp`

---

## 1. 总原则

### 1.1 工作分工
- 本地机器负责：
  - 写代码
  - 改配置
  - 写文档
  - 小规模本地自测
  - `git commit` / `git push`
- 远程主机负责：
  - 数据探测
  - 蒸馏 dry run
  - 大规模数据生成
  - SFT / RL 训练
  - 长时间运行任务

### 1.2 单一真源
以下文件是当前实现的硬约束来源，修改前必须确认相互一致：
- `data/schema_v1_1.md`
- `experiment_plan_v2.md`
- `config/pools.yaml`
- `config/sft_recipe.yaml`
- `scripts/schema_validator.py`

### 1.3 Benchmark 污染红线
凡是主表 eval benchmark，一律不允许进入：
- SFT
- RL
- distillation seed
- in-context examples

当前主线规则以 `experiment_plan_v2.md` 为准。

---

## 2. 标准开发流程

### Step 0: 本地进入项目

```bash
cd ~/Desktop/multi-agent-nips26
```

### Step 1: 修改代码或配置

常见修改位置：
- schema / 约束：`data/`
- recipe / pools：`config/`
- 数据脚本：`scripts/`
- 实验计划：`experiment_plan_v2.md`

### Step 2: 本地自检

至少做下面几项：

```bash
python3 scripts/schema_validator.py
python3 - <<'PY'
import yaml
with open('config/sft_recipe.yaml', 'r') as f:
    cfg = yaml.safe_load(f)
print('datasets:', len(cfg.get('datasets', [])))
print('target:', cfg.get('target_samples'))
PY
```

如果改的是 probe / distill / recipe，建议额外跑：

```bash
python3 scripts/availability_probe.py --help
python3 scripts/distill.py --help
```

### Step 3: 查看变更

```bash
git status
git diff --stat
```

### Step 4: 提交

```bash
git add .
git commit -m "feat: update validator and recipe"
```

### Step 5: 推送到远程仓库

```bash
git push
```

---

## 3. 服务器侧标准更新流程

### Step 1: 登录服务器

示例：

```bash
ssh user@your-server
```

如果有端口：

```bash
ssh -p 2222 user@your-server
```

### Step 2: 进入项目目录

```bash
cd ~/multi-agent-nips26
```

如果项目尚未克隆：

```bash
git clone <your-repo-url>
cd multi-agent-nips26
```

### Step 3: 拉取最新代码

```bash
git pull
```

如果你担心本地改动冲突：

```bash
git status
```

确认远程机器没有脏改动后再 `git pull`。

---

## 4. 服务器首次准备

### 4.1 Python 环境

推荐至少确认：

```bash
python3 --version
pip --version
```

### 4.2 安装基础依赖

按项目实际依赖补齐，最低建议：

```bash
pip install pyyaml datasets huggingface_hub
```

如果后续需要 parquet：

```bash
pip install pyarrow pandas
```

### 4.3 可选：新建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
```

---

## 5. 服务器预检查流程

在真正蒸馏和训练前，严格按顺序执行。

### 5.1 运行 schema validator

```bash
python3 scripts/schema_validator.py
```

预期：
- self-test 全过

### 5.2 运行 availability probe

```bash
python3 scripts/availability_probe.py
```

预期输出：
- `data/probe_report.json`
- 如果实现了 markdown summary，则还有 `data/probe_report.md`

重点检查：
- 是否有 dataset 无法 `load_dataset`
- split 是否存在
- 抽样字段是否符合预期
- 是否存在需要替换的数据集，如 BBH / 某些镜像不稳定项

### 5.3 检查 recipe

```bash
python3 - <<'PY'
import yaml
with open('config/sft_recipe.yaml', 'r') as f:
    cfg = yaml.safe_load(f)
print('target_samples =', cfg.get('target_samples'))
print('datasets =', len(cfg.get('datasets', [])))
PY
```

### 5.4 检查 pools

```bash
python3 - <<'PY'
import yaml
with open('config/pools.yaml', 'r') as f:
    cfg = yaml.safe_load(f)
print('models =', len(cfg.get('models', [])))
print('skills =', len(cfg.get('skills', [])))
PY
```

---

## 6. 蒸馏执行顺序

### 6.1 先做 30 条 dry run

不要直接全量跑 41.5k。

建议先做：
- 每个大 domain 抽 2-4 条
- 总计约 30 条
- 覆盖：
  - multi-hop
  - lazy
  - math
  - code
  - tool
  - long-context

示例命令（以最终 `distill.py` 参数为准）：

```bash
python3 scripts/distill.py \
  --recipe config/sft_recipe.yaml \
  --pools config/pools.yaml \
  --limit 30 \
  --output data/dryrun_30.jsonl
```

### 6.2 dry run 后的检查项

必须检查：
- schema valid rate
- repair 比例
- lazy 样本是否出现
- route 的 model / skill 分布是否正常
- 是否出现大量奇怪 pair
- 是否出现空 obs / 缺失 final_answer / round 错乱

### 6.3 小规模 pilot

30 条通过后，再跑：

```bash
python3 scripts/distill.py \
  --recipe config/sft_recipe.yaml \
  --pools config/pools.yaml \
  --limit 300 \
  --output data/pilot_300.jsonl
```

### 6.4 全量蒸馏

只有当以下条件都满足时才允许全量：
- dry run 通过
- pilot 通过
- probe 无阻塞项
- 成本估计可接受

然后再运行全量：

```bash
python3 scripts/distill.py \
  --recipe config/sft_recipe.yaml \
  --pools config/pools.yaml \
  --output data/sft_phase1_full.jsonl
```

---

## 7. 训练前检查

在开始 SFT / RL 前，先确认以下文件存在：

- `data/schema_v1_1.md`
- `experiment_plan_v2.md`
- `config/pools.yaml`
- `config/sft_recipe.yaml`
- `scripts/schema_validator.py`
- `scripts/availability_probe.py`
- `scripts/distill.py`

并确认数据产物存在：
- `data/probe_report.json`
- `data/dryrun_30.jsonl`
- `data/pilot_300.jsonl` 或同类文件

---

## 8. 服务器长期运行建议

长时间蒸馏、训练、评测，不要直接挂在交互 SSH 上。

推荐使用：
- `tmux`
- `screen`
- `nohup`

### 8.1 tmux 示例

```bash
tmux new -s router
```

在 tmux 里运行：

```bash
python3 scripts/distill.py ...
```

分离：

```bash
Ctrl-b d
```

重新进入：

```bash
tmux attach -t router
```

---

## 9. 如果 Git 不方便：tar.gz 快照流程

如果远程机不能直接拉 git，或者你只想传固定快照，可以用压缩包。

### 9.1 本地打包

```bash
cd ~/Desktop/multi-agent-nips26
mkdir -p exports
tar czf exports/router_snapshot.tar.gz \
  data \
  config \
  scripts \
  experiment_plan_v2.md
```

### 9.2 传到远程

```bash
scp ~/Desktop/multi-agent-nips26/exports/router_snapshot.tar.gz user@server:~/router/
```

### 9.3 远程解包

```bash
ssh user@server
cd ~/router
tar xzf router_snapshot.tar.gz
ls -la
```

---

## 10. 常见问题

### Q1. `git pull` 冲突怎么办？

先看远程是否有脏改动：

```bash
git status
```

如果有非必要改动，先备份或提交后再拉。

### Q2. HF 数据集下载失败怎么办？

先看：
- 是否需要登录 `huggingface-cli login`
- 是否 split 写错
- 是否镜像本身不可用

然后在 `probe_report.json` 里把它标记为：
- `FAIL`
- 或 `PASS_WITH_WARNINGS`

再按 `experiment_plan_v2.md` 的 deferred / replacement 策略替换。

### Q3. dry run 通过率低怎么办？

先不要扩样本。

先排查：
- prompt 是否稳定
- schema validator 拦了哪些 rule
- teacher 是否常漏 `verify`
- route / obs 对齐是否出错

### Q4. 大规模任务跑一半 SSH 断了怎么办？

所以默认使用：
- `tmux`
- `screen`
- `nohup`

不要把正式任务直接挂在裸 SSH 交互会话上。

---

## 11. 推荐的实际执行顺序

每次开始新一轮实验，按下面执行：

1. 本地改代码
2. 本地跑 validator / 基础 sanity check
3. `git add && git commit && git push`
4. 服务器 `git pull`
5. 跑 `availability_probe.py`
6. 跑 30 条 dry run
7. 人工 review
8. 跑 300-500 条 pilot
9. pilot 通过后全量蒸馏
10. 再进入 SFT / RL

---

## 12. 当前建议

当前项目已经完成：
- schema 锁定
- benchmark split 锁定
- pools / recipe 基本成型
- validator 已就位

因此接下来默认顺序是：

1. 完成 `availability_probe.py`
2. 跑 probe
3. 完成 `distill.py`
4. 跑 30 条 dry run
5. 再决定是否进入全量生成

不要跳过 dry run 直接全量蒸馏。
