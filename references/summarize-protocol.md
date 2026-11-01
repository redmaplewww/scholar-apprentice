# 归纳沉淀协议（K 阶段执行细则）

适用：任何需要"把领域知识从论文里归纳成结构，并持续维护"的任务。
机械执行体：`scripts/summarize_cli.py`；领域内容一律放 `domain-packs/`。

## 一、三层两区

| 层 | 内容 | 写入方式 |
|---|---|---|
| 证据层（`papers.db`） | 贡献记录、实践记录、批评记录、论文元数据 | AI 自动，只增不改，带出处 |
| 结构层（`structure.db`） | 改进轴、演化边、组合、耦合 | 派生；候选只在工作台，正式需审查 |
| 规范层（`structure.db`） | 基线、必测算例、必报参数、红线、贡献表述 | 同上，红线必须人审 |

"两区"指草稿区与正式区：`axes.status` 为 `candidate` 的是草稿（只在工作台出现），
`stable` 才是正式。所有转正式都必须经过一条 `proposals` 记录。

## 二、一个工作单元的完整流程

1. **复盘**（`reflect`）：工作单元结束后，Agent 写 0–3 条候选，附 `paper_id` / `evidence` / `quote` / `affects`。
2. **沉淀门**（脚本自动）：
   - G1 新：与已有轴、条款、边的余弦 ≥0.82 视为重复 → 合并为补证。
   - G2 据：`paper_id` 必须存在；带 `quote` 时逐字命中原文（`load_fulltext`：Zotero 全文索引优先，退回摘要）。
   - G3 影响：`affects` ∈ `redteam|control_group|assignment|teaching|clause|route|hole`。
   - G4 稳：`sources ≥ 2` 或显式 `single_observation=true`。
   未过门的进 `unadopted`，不进队列。
3. **审查 Agent**（`review`）：A1–A5，见 `review-agent.md`。
4. **去向**：静默入库 / Agent 审后入库 / 升级人审 / 驳回 / 待补来源。
5. **渲染**（`render`）：页面由库生成。

## 三、冷启动 C1–C7

| 步 | 命令 | 说明 |
|---|---|---|
| C1 语料 | `ingest zotero --collection <名> --limit 300` | 优先用用户自己的收藏夹与标签；不够再补开放获取 |
| C2 抽取 | `workorder extract --limit 10` → 填 → `commit extract --file` | 一批别超过 10 篇，`evidence` 必须能定位 |
| C3 开放归纳 | `induce` | **不要先看用户假设**；轴名是占位符，先看结构对不对 |
| C3′ 命名 | `workorder naming` → `commit naming` | 按成员内容起领域名、写定义 |
| C4 成链 | `induce`（含 edges/combos/couplings） | 需要贡献记录填了 `addr_technique` 与 `technique` |
| C5 挖条款 | `induce`（含 clauses） | 阈值在 `induce.mine_clauses` 顶部 |
| C6 对照 | `eval --hypothesis domain-packs/<领域>/hypothesis.yaml` | 输出 recalled/missed/extra |
| C7 审核 | `queue list` → `queue approve` | 一次性过一遍，通过即转正式 |

C6 的三种结果：两边都有 → 对齐（用用户命名）；用户有、AI 没有 → 开缺口单；AI 有、用户没有 → 标"建议新增轴"。

## 四、日常维护

- **触发**：新论文入库、一次会诊、一轮探索、一次实验结束、用户手改页面。
- **不做**：定时周报、批量补抽。没有新东西就写"零沉淀"。
- **体检**（`healthcheck`）：13 类信号，见 `health.signals`；`--act` 才生成提案与缺口单。
- **批次**：一次最多处理 5 个结构操作；队列满 20 条提示一次。

## 五、缺口单

| 类型 | 关闭条件 |
|---|---|
| thin_evidence | ≥2 个独立课题组，或检索穷尽 |
| broken_chain | 补上入边并附页码，或标"无明确前身" |
| unknown_side_effect | ≥1 条批评记录，或检索穷尽 |
| evaluation_gap | 找到已有结果，或实验完成 |
| coverage_gap | 映射成功，或开新轴候选 |

`healthcheck --close <id> --status closed|exhausted`；
`healthcheck --escalate <id>` 把检索穷尽的缺口升级为"疑似研究空白"，转探索引擎，仍需 E6 查重。

## 六、验收

| 题 | 命令 | 线 |
|---|---|---|
| Q1 盲归纳找回 | `eval --hypothesis ...` | 找回 ≥3/4，未找回要能解释 |
| Q2 覆盖率 | `eval` → `Q2_unassigned_rate` | ≤15%（真实库） |
| Q3 归轴正确率 | 人工抽检 20 条 | ≥90% |
| Q4 时间切分回放 | `replay build --cutoff <年>` | 新路线 2 次体检内进队列 |
| Q5 稳定性 | 回放报告 `Q5_flip_count` | 0 |
| Q6 补缺有效性 | `replay delete-recover` | 缺口能被识别；真补需联网 |
| Q7 沉淀克制 | `eval` → `Q7_zero_ratio` | ≥0.5，单月中位数 0–1 |
| Q8 审查质量 | `eval` → `Q8` | A1 ≥98%，升级率 ≤20% |

## 七、红线

1. 证据层的 `evidence` 不允许空；只有摘要的论文标 `abstract_only`，不得据此判定"解决了什么"。
2. 归轴只由脚本算、Agent 复核；**不得**让 Agent 直接从预设轴表里挑。
3. 结构变更必须走 `proposals`，保证可回滚；用户手改优先于任何自动提案。
4. 回放结果与一次归纳的结果不一致时，先查阈值与顺序，不允许"解释掉了"。
5. 不声称"已搜全"。缺口关闭时如实记录检索路线与停止原因。
