# 学术学徒（scholar-apprentice）

一个给 AI 用的研究工作流 skill：**先证明工具可信，再把领域知识沉淀成结构**。
本体只有流程、门控与执行脚本，不含任何领域知识；领域内容全部放在 `domain-packs/`。

面向硕博生做计算/实验类研究：从零学一个领域 → 找创新点 → 搭管道并验收 → 跑自己的算例 → 沉淀成可复用的 SOP。

---

## 它解决的两个具体问题

**问题一：自产数值和研究结论之间隔着一台仪器。**
仪器没验过时，"工具错了"和"真有发现"在产物上完全同形，事后无法区分。
本 skill 把"什么时候必须先验收、验收不过怎么办、每个偏差该谁去修"变成可执行的阶段门（I 阶段），而不是靠自律。

**问题二：领域知识只剩一堆论文卡，没有结构。**
读了 200 篇仍然说不清"这个领域的方法由哪几个可改进的部件组成、各自怎么演化、做研究该守什么规矩"。
本 skill 的 K 阶段把论文归纳成**改进轴 / 演化边 / SOP 条款**三层结构，并在日常使用中无感维护。

---

## 六个阶段

| 阶段 | 做什么 | 门 |
|---|---|---|
| **S** 自学 | 把领域学到可验收：课程表 / 知识总账 / 能力画像 | 已知答案题型 ≥80% |
| **T** 研讨 | 导读、精读、考核、苏格拉底式追问、反转让你教它 | 用户合卷复述零关键失配 |
| **E** 探索 | 虫群搜索 + 九算子找缺口，红队后交用户确认，再查重 | 用户确认 + 撞车定级 |
| **I** 仪器落成 | **先证明工具可信，再拿它的数字说事** | I1–I3 全过，或收缩覆盖范围 |
| **R** 取证 | 预注册 → 跑算例 → 判决 → 归因 | 每条结论挂仪器卡 ID |
| **K** 沉淀 | 归纳领域结构、生成 SOP、抽红线、补回归测试 | Q1–Q8（见下） |

阶段之间不是流水线：E 的缺口常需 I 验证，R 暴露的仪器问题回 I，K 的教训回写 I 的检查表。

---

## 快速开始

```bash
git clone <this-repo> scholar-apprentice
cd scholar-apprentice
python scripts/selfcheck.py                     # 0 全过：文件、schema、领域包、两套测试
cd scripts && python -m unittest -q test_summarize
```

依赖：Python ≥3.10，标准库即可；`PyYAML` 用于读取领域包与假设文件（`pip install pyyaml`）。
Zotero 集成可选（见下文）。

### 装成 skill

客户端从 `~/.agents/skills/` 读 skill。推荐目录联接，保证改动即时生效：

```bash
# Windows（免管理员）
cmd //c mklink /J "%USERPROFILE%\.agents\skills\scholar-apprentice" "<repo路径>"

# macOS / Linux
ln -s "$(pwd)" ~/.agents/skills/scholar-apprentice
```

### 新领域开工

```bash
python scripts/init_workspace.py 领域/weno --domain weno
cd 领域/weno

WS=.
python ../../scripts/summarize_cli.py --ws $WS init --domain weno \
  --core-question "间断附近无振荡与光滑区高阶精度之间的矛盾" \
  --principles "光滑区退化为最优线性格式，间断附近退化为选模板" \
  --scope "有限差分/有限体积的 WENO 型重构"
```

---

## K 阶段：领域归纳与学术 SOP（核心）

### 三层两区

```
证据层（papers.db，免审、只增不改、带出处）
   贡献记录 · 实践记录 · 批评记录
        │ 归纳（聚类 / 统计 / 引用关系）
        ▼
结构层（structure.db）   改进轴 · 演化边 · 组合 · 耦合
        │ 挖掘（频率 + 批评与反驳）
        ▼
规范层（structure.db）   基线 · 必测算例 · 必报参数 · 红线 · 贡献表述
```

- **草稿区**：`status=candidate` 的轴与条款只在工作台可见。
- **正式区**：`status=stable/active` 才出现在正式页面。
- **结构不预置**：轴由脚本聚类、Agent 命名；用户给的分解只作为 `hypothesis.yaml` 对照，
  **不参与归纳**——否则只是在验证自己的假设。

### 沉淀来自复盘，不为沉淀而塞

每完成一个工作单元（读完一篇论文 / 一次会诊 / 一轮探索 / 一次实验 / 一次红队），
AI 必须回头审自己的产出，写 0–3 条候选。**零沉淀是正常结果**；连续几个单元都挖出三条，
说明门槛松了。

四道沉淀门（`scripts/summarize/reflect.py`）：

| 门 | 判据 | 不过时 |
|---|---|---|
| G1 新 | 与已有轴/条款/边余弦 ≥0.82 视为重复 | 合并为"补证" |
| G2 据 | `paper_id` 存在；有 `quote` 时必须逐字命中原文 | 丢弃，记未采纳日志 |
| G3 影响 | 必须声明会影响哪个判断（红队/对照组/归轴/教学/条款/路线/缺口） | 丢弃 |
| G4 稳 | 来源 ≥2，或显式标为"单例观察" | 降级为观察 |

### 审查 Agent（A1–A5）

独立上下文，只看**提案、证据原文、正式结构快照**，不看提案是怎么想出来的。

| 检查 | 判据 | 不过时 |
|---|---|---|
| A1 证据可回溯 | 引文去空白后逐字命中（确定性字符串比对） | 驳回；无全文转 `needs_source` |
| A2 结论不过度 | 出现"普适/所有/必然/最优"且来源 <3 | 自动加限定语后通过 |
| A3 一致性 | 同轴同类条款余弦 >0.6 且否定极性相反 | 转争议单，不覆盖 |
| A4 必要性 | 与待审提案余弦 >0.9 | 合并去重 |
| A5 风险分级 | 判断题归人，常规项归 Agent | 见下 |

| 去向 | 适用 | 批准者 |
|---|---|---|
| 静默入库 | 证据记录、补证、统计更新 | Agent，抽样质检 |
| Agent 审后入库 | 新增演化边、技术族补充、描述性条款 | Agent，可撤销 |
| 升级人审 | 新轴晋升、拆分合并、红线条款、平行范式、与钉住内容冲突 | 你 |
| 驳回 / 待补来源 | A1 失败 / 无全文 | Agent / 挂起 |

目标：升级率 ≤20%，人的时间只花在判断题上。

### 页面就是界面

渲染产物可直接改：

- 页面末尾有 `<!-- user:begin -->` 区块，块内以 `- ` 开头的行会被吸收成 `source=user` 的批注；
- 路线页 frontmatter 的 `name:` / `definition:` 被改 → 直接生效并 `pinned=1`，后续渲染不覆盖；
- 其余部分每次渲染重写（库是唯一真相源）。

---

## 常用命令

```bash
CLI=scripts/summarize_cli.py
WS=领域/weno

# 语料
python $CLI --ws $WS ingest zotero --collection WENO --limit 300    # 可选，见 Zotero 集成
python $CLI --ws $WS ingest json --file papers.json                 # 手动元数据

# 抽取：工单 → Agent 逐篇填 → 提交
python $CLI --ws $WS workorder extract --limit 10 > order.json
python $CLI --ws $WS commit extract --file filled.json

# 归纳与命名
python $CLI --ws $WS induce
python $CLI --ws $WS workorder naming > naming.json
python $CLI --ws $WS commit naming --file naming_filled.json

# 复盘（每个工作单元收尾）
python $CLI --ws $WS workorder reflect --unit paper:doi:xxx > refl_template.json
python $CLI --ws $WS reflect --unit paper:doi:xxx --file refl.json

# 审查与审核
python $CLI --ws $WS review
python $CLI --ws $WS queue list
python $CLI --ws $WS queue approve pr-xxxxxxxx --by user
python $CLI --ws $WS queue revert  pr-xxxxxxxx
python $CLI --ws $WS queue snapshot --reason "审核前"

# 体检与缺口
python $CLI --ws $WS healthcheck            # 只看信号
python $CLI --ws $WS healthcheck --act      # 生成提案与缺口单
python $CLI --ws $WS healthcheck --holes
python $CLI --ws $WS healthcheck --close h-xxx --status exhausted
python $CLI --ws $WS healthcheck --escalate h-xxx     # 升级为"疑似研究空白"

# 渲染与验收
python $CLI --ws $WS render
python $CLI --ws $WS eval --hypothesis domain-packs/weno/hypothesis.yaml
python $CLI --ws $WS replay build --src $WS --dest $WS-replay --cutoff 2012
python $CLI --ws $WS replay delete-recover --n 2
```

产物：

```
<领域>/
├── db/papers.db        # 证据层
├── db/structure.db     # 结构层 + 规范层 + 提案/缺口/审查留痕
├── db/snapshots/       # 审核前快照
└── vault/
    ├── 领域地图.md
    ├── 路线/axis-XX.md
    ├── 领域SOP.md
    └── 工作台.md
```

---

## 验收 Q1–Q8

| 题 | 怎么测 | 线 |
|---|---|---|
| Q1 盲归纳找回 | `eval --hypothesis` | 强匹配 ≥3/4；弱匹配需人工核 |
| Q2 覆盖率 | `eval` 的 `Q2_unassigned_rate` | ≤15%（真实库） |
| Q3 归轴正确率 | 人工抽检 20 条 | ≥90% |
| Q4 时间切分回放 | `replay build` | 新路线 2 次体检内进队列；看 `over_merged/over_split` |
| Q5 稳定性 | 回放报告 `Q5_flip_count` | 0 |
| Q6 补缺有效性 | `replay delete-recover` | 缺口能被识别（真补需联网） |
| Q7 沉淀克制 | `eval` 的 `Q7_zero_ratio` | ≥0.5，单月中位数 0–1 |
| Q8 审查质量 | `eval` 的 `Q8` | A1 ≥98%，升级率 ≤20% |

参考结果（合成 WENO 语料 30 篇）：Q1 查全 1.0 / 强匹配 0.75、Q2 0.20、回放 Q5 0、
Q8 A1 通过率 1.0 / 稳态升级率 0.155。回放在小语料上会过度合并（4 轴 vs 3 轴），
指标如实报出，用于调阈值而非掩盖。

---

## 目录结构

```
scholar-apprentice/
├── SKILL.md                     # 触发条件、阶段索引、K 阶段命令、反模式
├── README.md
├── references/
│   ├── stages.md                # 六阶段细则
│   ├── stage-I-instrument-commissioning.md
│   ├── summarize-protocol.md    # 归纳协议（三层两区、C1–C7、沉淀门、验收）
│   ├── review-agent.md          # 审查 Agent 的判据、去向、抽查
│   └── meta-lessons.md          # 跨领域元经验（逐领域积累）
├── scripts/
│   ├── init_workspace.py        # 铺工作区
│   ├── phase_gate.py            # I 阶段门（exit code 0/1/2）
│   ├── selfcheck.py             # 自检
│   ├── summarize_cli.py         # K 阶段 CLI
│   ├── summarize/               # 13 个模块：db / evidence / induce / reflect /
│   │                            #   review_agent / queue / health / render / replay /
│   │                            #   zotero_bridge / textutil / fixture
│   ├── test_phase_gate.py
│   └── test_summarize.py        # 端到端 11 项
├── assets/workspace-template/   # 研究档案模板
└── domain-packs/
    ├── weno/                    # hypothesis.yaml（四轴假设，仅对照用）
    └── fracture/                # pack.yaml + hypothesis.yaml + 仪器验收卡
```

---

## 领域包机制

**判据：一段内容如果换个领域就不成立，它属于领域包。**

| 属于核心 | 属于领域包 |
|---|---|
| 阶段划分、门控、判据 | 已知答案的具体数值与出处 |
| 归纳协议、沉淀门、A1–A5 | 用户假设（`hypothesis.yaml`）、领域红线 |
| 模板（仪器卡 / 筛查矩阵 / 报告） | 事件定义、判读规则、算例清单 |
| 数字出生证规则 | 单位、取向、符号约定 |

新领域 = 新建 `domain-packs/<领域>/`，核心脚本不改。

---

## Zotero 集成（可选）

K 阶段可通过 `scripts/summarize/zotero_bridge.py` 直接调用一个只读 Zotero MCP
（双后端：Zotero 本地 API 优先，退回 `zotero.sqlite` 快照），取元数据、全文、
以及你读论文时的高亮批注。

1. 安装 Zotero 桌面版并启动一次；
2. 设置 → 高级 → 勾选「允许本机其他应用与 Zotero 通信」；
3. 用环境变量 `ZOTERO_MCP_SERVER` 指向你的 Zotero MCP 脚本
   （默认值 `D:\kb-services\zotero-mcp\server.py` 只是作者本机路径，请改）。

```bash
python $CLI --ws $WS zotero status
python $CLI --ws $WS zotero recent --days 30
python $CLI --ws $WS ingest zotero --collection WENO --limit 300
```

没有 Zotero 也能用：走 `ingest json` 或 PDF 导入，`zotero_bridge` 会返回明确的降级原因。

---

## 常见问题

**合出来的轴名字是英文术语或占位符？**
聚类只负责结构，命名交给 `workorder naming` → Agent 按成员内容起名。先看结构对不对，再命名。

**未归类比例偏高？**
先调 `summarize/induce.py` 顶部的 `DEFAULT_CLUSTER_THRESHOLD`（默认 0.28）与归轴余量（0.06）。
短词组（<8 词）的词面相似度天然低，抽取时要求 `target_text` 写成完整短语。

**回放显示 `over_merged=true`？**
小语料上先成形的少数轴会吸收后续记录。以真实库的 Q4 结果为准，把调好的阈值写回
`induce.py` 与 `references/meta-lessons.md`，不要为了让指标好看去改回放。

**审核队列被塞满？**
看 Q7（零沉淀率）与 Q8（升级率）。前者低说明沉淀门太松，后者高说明 A5 分级太保守。

**能直接改生成的页面吗？**
可以。user 区块原样保留；路线页 frontmatter 改名/改定义直接生效并钉住，其余部分会被重渲染覆盖。

---

## 开发与测试

```bash
python scripts/selfcheck.py                             # 文件齐备、schema、pack、两套测试
cd scripts && python -m unittest -v test_summarize      # 11 项端到端
cd scripts && python -m unittest -v test_phase_gate
```

设计约定：**统计、阈值、渲染、回放由确定性代码做；抽取、命名、写定义由 Agent 按工单做。**
这样回放与真实运行同源，不会出现"回放通过、实跑失败"。

---

## 状态与许可

阶段门（S/T/E/I/R/K 的流程与 I 阶段工具）与 K 阶段归纳流水线均已实现并自测通过；
`domain-packs/fracture` 是作者真实研究项目演化出来的参考包，数据不在本仓库。

**许可：专有许可，保留所有权利。** 见 [LICENSE](LICENSE)。要点：

- 不是开源许可。除在线浏览与法定合理使用外，不授予任何权利。
- 禁止复制、修改、翻译、演绎、分发、转售、再许可。
- 禁止任何商业使用（含商业产品、付费培训、咨询交付、SaaS/API 服务、内部商业流程）。
- **禁止用于训练、微调、蒸馏或评测机器学习/人工智能系统**，禁止纳入任何训练数据集。
- 如需授权，须取得权利人的书面许可。

如需完全禁止他人 fork，请将本仓库置为私有——公开仓库下平台条款仍允许在平台内创建 fork。
