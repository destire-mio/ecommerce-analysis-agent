# 电商店铺经营分析助手 — 当前实现

> 工作区：`agent-workspace`
> 一句话：用户用自然语言问经营问题（查数 / 归因 / 口径解释 / 出报告），agent 在自店 SQLite 库上自主取数、在平台侧通过 MCP 工具取大盘与流量，给出带证据编号、可审计、区分「合法空集 / 数据缺失 / 查询失败」三态的结论。
> 项目方法：AGENTS.md 四轮螺旋（材料 / Runtime / 评测 / 承载），每个场景点亮一圈。

---

## 一、总体架构（当前版）

```
用户 query
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│  预处理层（纯代码，零 LLM）                                  │
│    清洗 → 量材(全列清单体积) → 组装材料包                    │
│    小库: 目录+全部列名整本给 ｜ 大库: 只给表名清单           │
│    追加: 数据覆盖截止日 + 技能索引(3 张卡)                   │
│    (阈值 2 万字符; 列值画像/值索引均为离线预建缓存)          │
└────────────────────────────────────────────────────────────┘
     │ 材料包
     ▼
┌────────────────────────────────────────────────────────────┐
│  agent 层（agent_loop.py，LLM + function calling）          │
│    system prompt(冻结模板, 含 {database_context} 占位符)    │
│    六原则: 语义保持/数据保真/有据可依/诚实交付/三态/证据标注 │
│    循环 ≤15 轮: LLM 出 tool_calls → 驱动器执行 → 结果回喂    │
│    无 tool_calls 且无未解错误 → 该轮正文即最终 answer        │
│    答案机械校验(无分隔符/无Qn/粗体/含糊引用) → 打回重答 ≤3   │
└────────────────────────────────────────────────────────────┘
     │ 结构化参数 / SQL / MCP 调用
     ▼
┌────────────────────────────────────────────────────────────┐
│  工具层（零 LLM，确定性）                                    │
│    自店(SQL): peek_table / peek_values / find_value          │
│              query_db(六槽位意图) / execute_sql              │
│    知识/交互: read_skill / ask_user                          │
│    计算/交付: calculate(6 具名公式) / save_report(对账+Word) │
│    平台(MCP): get_market_overview / get_shop_traffic /       │
│              get_funnel —— 独立进程, 物理隔离               │
│    门禁: SELECT白名单/单语句/写词黑名单/值索引/EXPLAIN        │
│    执行: 3s 超时 + PRAGMA query_only 双层只读                 │
└────────────────────────────────────────────────────────────┘
     │
     ▼
  answer（结论 + ###EVIDENCE### + 证据明细）；错误→带自纠线索回喂→修正重试
```

**数据物理隔离**：自店交易库 `data/ecommerce/ecommerce.sqlite`（orders/items/products，SQL 可达）与平台库 `data/platform/platform_data.sqlite`（大盘/渠道流量/漏斗，**仅 MCP 可达**）互不越界。

---

## 二、核心设计（含设计理由）

### 1. 机器管确定性，agent 管理理解（职责铁律）

| 层 | 干什么 | LLM？ |
|----|--------|-------|
| 预处理层 | 洗、量、装材料 | 无 |
| agent 层 | 理解问题、判断看什么、填意图/写 SQL、选 MCP 工具 | **有** |
| 工具层 | 校验、渲染、执行、计算、对账 | 无 |
| 判分层 | 行集合比对 / 数字对账 | 无 |

被否决并内化的方案（演进记录）：关键词预召回、前置简单/复杂分类器、复杂题预改写——全部改由 agent 运行时自判断。

### 2. 材料包 = 渐进式披露

- 开局只给目录（小库含全部列名 / 大库仅表名，由体积阈值机械决定，无智能参与）；
- 细节按需获取：结构→peek_table，取值→peek_values，字面定位→find_value；
- 追加「数据覆盖截止日」与「技能索引」：截止日防超范围编 0，技能索引引导先读卡再动手；
- 理由：列名层必须全量（判断的基础），内容层必须限量（54 万字符 vs 1500 字符的实测教训）。

### 3. 工具边界（名字层全量，内容层限量）

- `peek_table`：外键 + 每列一行统计（不同值数/非空率/平均长度），零预选——"哪些格子关键"由 agent 看着数字判断；
- `peek_values`：单列真值 ≤50 个 + keyword 筛选（total_distinct 有数）；
- 每个 `unknown_*` 错误都附可用清单——错误是路标不是死信。

### 4. 双模式查询（意图优先、SQL 兜底）

```
query_db(六槽位): metric / filters / group_by / order_by / limit / distinct
  渲染器保证: 列存在性校验(unknown_column 带清单)、字符串自动 COLLATE NOCASE
装不下六槽位(跨表 JOIN/交集/复杂条件) → agent 直接写 SQL 走 execute_sql
```

### 5. 知识进卡片（三张技能卡，零场景专属代码）

| 卡 | 作用 | 触发 |
|----|------|------|
| `diagnosis-sop` | 下降归因五步：总量对比→渠道拆解→大盘对照→漏斗定位→商品矩阵；证据分三级、停止规则、已知坑 | 做归因前必读 |
| `metrics-handbook` | 指标关系树、各指标口径/公式/拆解刀/易错坑、**未开放指标清单**（退款率/库存/利润/复购率） | 涉及指标口径前必读 |
| `report-sop` | 报告四段式、派生数必须用 calculate、落盘 save_report 对账 | 说"出报告"时必读 |

### 6. 门禁与值索引

- 门禁：SELECT 白名单 / 单语句 / 写词黑名单 / EXPLAIN 预检 / 字面值 vs 值索引（无索引库降级为仅 EXPLAIN）；
- 值索引：FTS5 trigram 侧库，**单值长 ≤200 字符才收**（教训：不设上限时 XML 大文本列曾炸出 2.2GB 索引）；
- 只读双层：应用级门禁 + `PRAGMA query_only`（引擎级，不共享故障点）。

### 7. 计算器与报告对账（确定性交付）

- `calculate`：6 具名公式 `delta / pct_change / share / contribution / avg / closure`；边界（基期 0、分母 0）返回"不适用"，**绝不返回 inf**；
- `save_report`：先把报告正文里每个数字与其所引 `(Qn)` 的账本记录逐一比对（容差 0.02），不过则返回 `rejected + issues` 让 agent 改；过了才转 Word 并写元数据。报告ID `r-<时间戳>-<随机尾>`。

### 8. 证据标注与三态

- 每个数字引用其真实来源 `(Qn)`，Qn **会话级连续**；`###EVIDENCE###` 之前是用户结论、之后是审计明细；
- 三态互不冒充：合法空集（答 0）/ 数据缺失（禁数字，说明缺什么）/ 查询失败（报错并自纠）；
- 超数据覆盖范围 → 答"未覆盖"，不答 0。

### 9. 会话与并发身份

- 会话即上下文：`sessions/<sid>/messages.json`（对话）+ `executions.json`（账本，Qn 跨轮连续）+ `runs.jsonl`；
- `claim_dir()`：时间戳 + 4 位随机尾 + **os.mkdir 独占认领**，同名当场 FileExistsError 换尾重试——同秒并发也不撞（真实事故修复，见 §五）。

### 10. 循环驱动

```
messages = [system, user(问题+材料包)]
每轮: LLM(temperature=0, function calling)
  ├─ 有 tool_calls → dispatch 执行 → role=tool 回喂 → 下一轮
  │    同一(工具+参数)错误连续 2 次 → 结果附加 hint="请换思路"
  ├─ 无 tool_calls 且有未解错误 → 提醒"先处理错误" → 继续
  └─ 无 tool_calls 且无错误 → 机械校验答案格式 → 过则结束，否则打回(≤3次)
预算 15 轮耗尽 → status=budget_exhausted，禁止编造答案
```

trace 事件：`start / session / mcp_connect / llm(含 token 记账) / tool_call / tool_result / execute / answer_validation / report / end`——可回放可审计。

---

## 三、代码地图

```
agent_loop.py            新版 agent 循环: 预处理/工具/循环/预算/证据编号/计算器/报告对账/MCP 桥
runtime.py               上一代单步 Runtime（仍在役）: 单次生成+门禁×5+重试
                         及离线基建: 列值画像 build_profile / 值索引 build_value_index
tools/platform_mcp.py    mock 平台数据 MCP server（官方 SDK, stdio, 3 工具）
tools/run_golden_v2.py   Golden Cases 批跑器（机械预检 + 人工判定列）
tools/run_exam.py        旧判分器(行集合比对) + 批跑器
tools/make_ecommerce_data_v2.py  自店演示数据生成（对齐 PRD 算例）
data/ecommerce/          自店库 + golden_cases_v2/s3 + metrics.yaml + skills/(3 卡)
data/platform/           平台 mock 库（物理隔离）
data/profiles/           列值画像 + 值索引（离线缓存）
results/golden_v2/       G1~G7 记录（7/7 PASS）
results/golden_s3/       S3-1~S3-4 记录（4/4 PASS）
reports/                 save_report 落盘的 Word + 元数据 JSON
badcase.md               失败台账（考卷阶段 + 电商场景真实案）
runs/                    trace 证据库（每次运行一个 run-<时间戳>-<随机尾>/trace.jsonl）
sessions/                会话消息 + 账本
rounds/                  STATE.md(交接快照) / history.md(本文) / backlog-mdl.md(下一圈设计)
```

---

## 四、评测现状

| 考卷 | 分数 | 备注 |
|------|------|------|
| 电商 Golden v2（S2 归因） | **7/7 PASS** | G1 查数回归 / G2 对比 / G3 复合归因 / G4 净贡献 125% / G5 流量结构 / G6 缺数-漏斗 / G7 未开放指标 |
| 电商 Golden S3（口径解释） | **4/4 PASS** | 基线查数 / 追问退款边界 / 直问口径 / 直问表来源 |
| Spider 20 题 | 15/20 | deepseek-chat 旧基线；题 4 修复后回归 PASS |
| BIRD 20 题 | 8/20 | 修正三 bug 后诚实基线；坏题/口径题人工裁决后真实水平更高 |

判分口径：数字/语义词组特征（不绑字面）+ 必调工具（从 trace 查）+ 禁止项；`pass = 全部 check ∧ status=ok`。

---

## 五、真实事件台账（详见 badcase.md）

1. **golden_cases 题7 标准答案错误**：agent 答 P0004=20,629 元（按商品聚合），golden 说 P0016=1,541 元（实为"单笔最大明细行"，生成器口径错）。SQL 查证后裁决 **agent 对、标准答案错**，修正 golden v2。教训：标准答案也要被挑战；生成 Golden 时「问题语义→统计口径」必须显式。
2. **同秒并发撞 session id**：G9/G10 同秒起跑，`s-YYYYMMDD-HHMMSS` 只精确到秒 → G10 恢复进了 G9 会话。修复 `claim_dir()`（时间戳+随机尾+独占创建），同秒 50 目录全唯一。
3. **approved_sql.json 漂移**：S3 运行中 agent 报"8,000 单 / 单均 100"，与该文件写的"4,000 单 / 单均 200"冲突；直查库证明 **agent 对、快照文件旧**（品类名也漂移）。教训：批准材料本身会漂移，需比快照更强的锚。
4. **口径答案缺"指标负责人"**：PRD L01 要求口径含负责人，metrics.yaml 有 owner 字段，但手册卡未写 → agent 读不到。材料缺口，记账待补。

---

## 六、待办队列（按优先级，详见 STATE.md / backlog-mdl.md）

1. **收口欠账**：history 已更新（本文）；答卷人工终审仍待填（`results/round3_manual_eval.md`）
2. **LMDI 乘法贡献引擎（T11）**：确定性公式引擎，LLM 手算不可信，未实现
3. **MDL 语义层 + dry_plan**（下一圈，设计见 `rounds/backlog-mdl.md`，方案将同步进 AGENTS.md）
4. 会话条件结构化（条件卡）——当前消息直通，继承靠 LLM 语感
5. 上下文压缩（对话长了裁剪）
6. 最小 UI（工作台/查看依据）——用户明确"完全做完以后再说"
7. S3~S8 场景（S3 口径 / S5 报告 / S7 治理 / S4 图表 / S6 运维 / S8 权限）——每场景一个新螺旋圈

---

## 七、使用方式

```bash
# 必须用 venv（mcp SDK 装在 .venv，系统 python 没有）
source ~/.zshrc   # DEEPSEEK_API_KEY
.venv/bin/python agent_loop.py --question "..." --db ecommerce --bench ecommerce [--session s-xxx]
.venv/bin/python tools/run_golden_v2.py                    # 跑 v2 七题
.venv/bin/python tools/run_golden_v2.py --file golden_cases_s3.json   # 跑 S3 四题
.venv/bin/python tools/run_golden_v2.py G3-复合归因         # 单题
```

依赖：Python 3.11+、`openai`、`mcp`、`python-docx`、`DEEPSEEK_API_KEY`；数据离线。

---

## 八、设计铁律

1. **机器管确定性，agent 管理理解**——校验/渲染/执行/计算是代码；理解/挑选/判断是 LLM；两者不越界；
2. **给发现的能力，不给答案**——目录+工具+错误路标，不预选、不预判、不代翻；
3. **知识进卡片，机制进代码**——场景知识全在技能卡，零场景专属代码；
4. **数据是事实源，文档/快照只是假设**——冲突时查库裁决（已两次证明 agent 对、标准件错）；
5. **三态不冒充**——合法空集 / 缺数 / 失败，各有各的说法；
6. **证据标注**——每个数字挂真实来源 Qn，结论与审计分离；
7. **全程可观察**——每轮 LLM/工具/执行落 trace，含 token 记账；
8. **不回答比瞎回答好**——empty 是合法结果，预算耗尽如实报告，禁止编造。
