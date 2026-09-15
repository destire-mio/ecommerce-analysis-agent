# Agent Workspace

发现真实问题
→ 用第一性原理缩小问题
→ 给 AI 有边界的 Prompt
→ AI 先读现状，再执行小改动
→ 真实运行
→ 收集证据
→ 对抗性审查
→ 将失败回流为下一轮问题

项目按照四轮推进：

## 第 0 轮：侦察，可选

- 目标：了解当前项目如何工作，用户哪一步不顺。
- 产物：现状描述、一句用户故事、第一条可观察证据。
- 要求：只读检查，不修改代码。

## 第 1 轮：材料与能力入口

- 目标：完成一条“用户输入 → Tool/Provider → 结果”的链路。
- 产物：固定 Mock 结果、真实结果、明确失败结果。
- 要求：一次只解决一个用户可见问题。

## 第 2 轮：Runtime 编排

- 目标：处理多步任务、上下文连续、停止条件和恢复。
- 产物：一个可观察、可停止、可恢复的最小 Agent Runtime。
- 要求：使用 pi-coding-agent Skill。

## 第 3 轮：评测与证据

- 目标：证明结果可信，并处理失败。
- 产物：Golden Cases、失败归因、回流规则、成本预算。
- 要求：用同一个失败案例重新运行验证。

## 第 4 轮：全栈产品承载

- 目标：把 Runtime 放进真实产品。
- 产物：UI → IPC → Runtime → Tool → UI 的完整链路、打包冒烟和发布门禁。
- 要求：使用全栈产品承载 Skill；这一轮不增加业务功能。

每一轮都执行 8 个动作：

1. 观察：真实发生了什么？
2. 定义：用户哪一步不顺？
3. 拆解：问题属于材料、编排、评测还是承载？
4. 归因：最早卡在哪里？
5. 提示：让 AI 先读现状，再提出最小切片。
6. 执行：只修改一条用户可见路径。
7. 审查：从用户、故障或新手视角寻找失败。
8. 回流：通过才扩大，失败就回到对应板斧。

每轮结束必须回答：

1. 这次改变了用户什么行为？
2. 证据是什么？
3. 还没有解决什么？
4. 下一轮最小问题是什么？

---

# 下一圈方案（分层设计已固化，待开工）

> 状态：2026-09-14 讨论后按层固化；全部决策拍板前不实现
> 详细背景与来源：`rounds/backlog-mdl.md`（大淘宝 NL2MDL2SQL / WrenAI）
> 一句话：把「每次都可能写错的口径与 JOIN」从 agent 手里收走，固化成一张「外号 → 写好的 SQL」对照表，agent 只写外号，引擎负责展开。

## 一、要解决的真实问题

现在 agent 直接对原始表写 SQL：`status='paid'`、`shop_id=1`、JOIN、口径过滤全靠它每次自己拼。后果是——

1. **口径靠 LLM 自觉**：正确性依赖它记得住技能卡里的条件；
2. **不沉淀**：`approved_sql.json` 是死的、且已漂移（真实事故）；
3. **整包 dump schema**：没有「按问题取相关模型」；
4. **JOIN 手拼**：关系没声明，每次重写。

把「写死一次」的东西（口径 / 关系 / 计算字段）移出 LLM 的犯错面，让 agent 只填**参数**（时间窗、分组、筛选）。

## 二、目标与本圈范围

**分层（已固化，见 §3.6）：**

- 第 0 层 · 表名替换（外号 → SQL）
- 第 1 层 · 列改名(1A) ＋ 指标展开(1B，并入第 2 层)
- 第 2 层 · 指标展开(2A) / 比率派生(2B) / 恒等式校验(2C)

**本圈最小切片（先做第 0 层 + 第 1 层 1A）：**

1. 写 `data/ecommerce/mdl.yaml`：3 模型（`paid_gmv`/`paid_orders`/`products`）+ 粒度/主键/单位声明
2. 新工具 `dry_plan(modeled_sql)`：`sqlglot` 解析 → 外号展开成 CTE → 返回**展开后的真实 SQL**（不执行），进账本
3. 材料包加「可用语义模型/指标」索引（由 MDL 生成）
4. 验收：`SELECT SUM(gmv_amt) FROM paid_gmv WHERE ...` 展开正确 + 执行结果 = **800,000**

## 三、设计草案

**MDL = 一张「外号 → 事先写好的 SQL」的对照表**（本质是数据库视图：存一段 SQL、起个名、以后当表查）。agent 写查询时用外号，引擎把外号原地替换成那段 SQL。

### 3.1 对象

| 对象 | 是什么 |
|---|---|
| Model | 逻辑数据集：物理表（`table`）或一段 SQL（`ref_sql`） |
| Column | 暴露字段：可改名 / 可计算 / 主键 / 粒度声明 |
| Relationship | 可复用的 join 逻辑（`join_type` + 等值条件） |
| Metric | 具名聚合（`model` + `expression` + `unit`） |
| Unavailable | 声明"没有这个数据、禁止推断"（机器据此直接挡） |

### 3.2 例：`data/ecommerce/mdl.yaml`

```yaml
schema_version: 1
models:
  - name: paid_gmv                    # 外号
    description: 支付明细——仅 paid、仅本店、不抵减退款
    grain: order_item                 # 粒度：一行 = 一个商品行
    key: [order_id, product_id]       # 主键/去重键
    ref_sql: |
      SELECT i.order_id, i.product_id, i.category,
             i.amount AS gmv_amt, o.pay_time
      FROM order_items i JOIN orders o ON o.order_id = i.order_id
      WHERE o.shop_id = 1 AND o.status = 'paid'
    columns:
      - {name: order_id,  unit: 标识}
      - {name: product_id, unit: 标识}
      - {name: category,  unit: 文本}
      - {name: gmv_amt,   unit: 元}
      - {name: pay_time,  unit: 时间}
  - name: paid_orders
    grain: order                      # 粒度：一行 = 一张订单
    key: [order_id]
    ref_sql: |
      SELECT o.order_id, o.pay_time, o.status
      FROM orders o WHERE o.shop_id = 1 AND o.status = 'paid'
    columns:
      - {name: order_id, unit: 标识}
      - {name: pay_time, unit: 时间}
      - {name: status,   unit: 文本}
  - name: products
    grain: product                    # 粒度：一行 = 一个商品
    key: [product_id]
    table: products                   # 物理表，外号=同名
    columns:
      - {name: product_id,   unit: 标识}
      - {name: product_name, unit: 文本}
      - {name: category,     unit: 文本}
relationships:
  - {from: paid_gmv.product_id, to: products.product_id, join_type: MANY_TO_ONE}
metrics:
  - {name: 支付金额,   model: paid_gmv,    agg: sum,   column: gmv_amt, unit: 元}
  - {name: 支付订单数, model: paid_orders, agg: count, distinct: order_id, unit: 单}
ratios:                               # 比率：由已登记指标组合（2B）
  - {name: 单均金额, expr: 支付金额 / 支付订单数, unit: 元/单}
unavailable:
  - {name: 退款率, reason: 退款数据未接入}
  - {name: 库存,   reason: 库存快照未接入}
  - {name: 利润,   reason: 成本数据不存在；禁止从销售额推导}
  - {name: 复购率, reason: 无用户维度数据}
```

**粒度三处钉死**：模型声明 `grain`（一行是什么）；指标声明 `distinct`（去重键）与 `unit`；比率只许用**已登记指标**组合、禁裸写聚合。加载期校验"单位能算、粒度各自正确"（见 §3.6）。

**`unavailable` 的两个入口**：① 材料包列出未开放指标 → agent 不去编；② 万一 agent 写 `SELECT 退款率 FROM ...`，`dry_plan` 返回 `{"error":"unavailable_metric","name":"退款率","reason":"退款数据未接入"}` → agent 如实答"数据缺失"。把 Golden G7 从"靠自觉"升级成"机器挡"。

### 3.3 执行机制（第 0 层：CTE 展开）

```
agent 写"模型 SQL"（FROM paid_gmv / JOIN products）
  → sqlglot 解析（探针验证 30.18.0 可用）
  → 遍历语法树的表名位置（exp.Table），命中外号
  → 以 CTE 形式提升到语句最前：WITH paid_gmv AS (ref_sql), ...
  → 未命中的名字当物理表放行（探索仍可用）
  → 回写真实 SQL → 门禁 → 执行
```

**第 0 层规则（已定）：**

| 项 | 定案 | 理由 |
|---|---|---|
| 展开形态 | **CTE**（提到 `WITH` 开头） | 同外号只一份；保留外号名，可读可审计；SQLite 3.53 会自动内联，无物化性能坑 |
| 替换锚点 | 语法树表名位置 | 避免误伤列名/字符串 |
| 同外号多处引用 | 只生成一个 CTE | 去重 |
| 重名冲突 | 检测到 → 改名（必做） | 探针：同名 CTE 会生成非法 SQL |
| 嵌套（外号套外号） | **支持**：递归展开 + 查环 + 拓扑排序 | 探针：只展一层会漏内层 → 运行时表不存在 |
| 物理表型模型 | **不包 CTE**，直接用真表 | 包了等于自己 shadow 自己 |
| 列名对齐 | 加载期**强制** ref_sql 输出列 = 声明列 | 不等就报错；改签留到 1A |

### 3.4 技能卡分工（瘦身，不是删）

| 内容 | 现在在哪 | 改造后 |
|---|---|---|
| 支付金额口径（过滤/时间/聚合） | 手册卡 §1 + `metrics.yaml` | **搬进 MDL** |
| 支付订单数口径 + 去重 | 手册卡 §2 + `metrics.yaml` | **搬进 MDL** |
| 单均 = 金额÷订单数 | 手册卡 §3 | **搬进 MDL**（计算字段） |
| 指标关系树（归因骨架） | 手册卡 §0 | **留卡**（方法，非口径） |
| 跨品类订单数不可相加 | 手册卡 §2 坑 | **留卡**（MDL 表达不了"禁止"） |
| 访客数 / 转化率 | 手册卡 §4 §5 | **留卡**（平台 MCP + 跨源，MDL 够不到） |
| 未开放指标清单 | 手册卡 §6 §7 | 口径进 MDL `unavailable`，卡里保留说明 |
| 诊断五步 / 报告四段 | `diagnosis-sop` / `report-sop` | **完全不动** |

### 3.5 单一事实源铁律（防漂移）

口径绝不允许出现两份（现有 `metrics.yaml` + 手册卡已是隐患；再加 `mdl.yaml` 就是三份——正是 `approved_sql.json` 漂移病的根源）。

**决定（B 方案）**：MDL 是唯一权威，`metrics.yaml` 保留为**人读版**，但必须**由 MDL 单向生成**（脚本从 `mdl.yaml` 渲染），禁止手工双写。做不到生成，就删掉 metrics.yaml（A 方案），不留退化版。

### 3.6 分层设计与粒度/单位

**分层（一层一层点亮）：**

| 层 | 做什么 | 状态 |
|---|---|---|
| 第 0 层 | 表名替换：外号 → CTE | 本圈做 |
| 第 1 层 | 1A 列改名（CTE 列别名表）；1B 指标展开 | 本圈只做 1A；1B 并入第 2 层 |
| 第 2 层 | 2A 指标展开 / 2B 比率派生 / 2C 恒等式+闭合校验 | 另圈 |

**粒度/单位对齐（难点 1 的解法）**：把粒度钉死在三处，加载期机器校验，不靠人工。

1. 模型声明 `grain`（一行是什么）+ `key`（去重键）；
2. 指标定义自带 `distinct`（去重键）与 `unit`（单位）——agent 写指标名即自带正确粒度，永不用手写 `DISTINCT`；
3. 比率（2B）只许由**已登记指标**组合，**禁止裸写聚合**（`SUM`/`COUNT`）。

校验规则：

| 运算 | 规则 | 例 |
|---|---|---|
| 相加/相减 | 单位相同 | 元+元 ✅ ／ 元+单 ❌ |
| 占比 | 单位相同 | 元÷元、单÷单 |
| 比率 | 单位可不同 | 元÷单 = 元/单 |
| 数数量 | 看粒度 + 去重键 | 商品行粒度上要"订单数" → 按 `order_id` 去重 |

**与计算器 `calculate` 的边界**：同模型、能下推 SQL 的比率进 MDL（2B，自动跟分组/过滤）；**跨模型、跨源**（本地 SQL + 平台 MCP）的比率走 `calculate` 事后算。

**MDL 位置与读取**：`data/ecommerce/mdl.yaml`；`agent_loop.py` 启动用 `pyyaml` 读入编译成内存表；材料包（由它生成）列出指标索引，`dry_plan` 用它做展开。

## 四、落地路线（本圈只做第 1 格）

| 圈 | 做什么 | 难度 |
|---|---|---|
| **本圈** | 3 模型 + 1 关系 + 手写 `dry_plan` 展开器，跑通一条链路 | 小 |
| 下一圈 | 计算字段 + 指标恒等式校验（乘法恒等式） | 中 |
| 再下圈 | 上下文检索（按问题取相关模型，替掉整包 dump） | 中 |
| 后续 | NL→SQL 沉淀/召回、关系自动遍历、治理闭环 | 中~大 |

## 五、验收与回归（硬约束）

- **验收**：`dry_plan` 展开 SQL 正确 + 执行结果 = 800,000
- **回归**：现有 Golden Cases（v2 七题 + S3 四题）**必须不回归**
- 新增 Golden：模型查询 → 展开 SQL 正确 + 结果正确

## 六、决策记录

**已定（2026-09-14 讨论固化；标注※为实施后更新）：**

1. ※ **agent 输出 QuerySpec 点菜单**（metrics/dimensions/time_range/time_grain/filters/order_by/limit/compare），`LLM 不拥有 SUM/COUNT/DISTINCT/JOIN 决定权`。原"模型 SQL"方案(sqlglot 展开外号)降级为 sqlite 节点的**编译后端**，不浪费。
2. ※ **比率不必同模型**（原"必须同模型"是 MVP 限制，已放宽）：分子分母**各在自己模型聚合**，按公共维度对齐后相除（aggregate-then-align），从根上避免行放大。
3. ※ **平台指标以 provider 进语义层**（推翻早期"平台侧不进 MDL"）：mdl.yaml 声明 `provider: platform_mcp + mcp_tool`，MCP 工具只透传时间窗等参数，**维度分组一律 Python 侧做**；工具照旧注册（McpBridge 未动）。
4. ※ **维度交集校验**：请求维度必须对所有叶子模型可用，否则规划期报 `dimension_unavailable`（访客按品类拆非法）。
5. ※ **声明必须被机器验证**：MDL 主键加载期真查库验唯一性（假主键拒绝加载）；ref_sql 输出列必须等于声明列。
6. **数据层补 order_item_id 行级主键**（同订单同商品两行=两笔真实交易，各自有 id）；金额整数分/品类二义性/独立事实表视图：推迟或砍（见 M2 消化记录）。
7. **未开放指标** → MDL `unavailable` 声明，机器也挡。
8. **`metrics.yaml` 走 B**：MDL 权威，人读版由 `tools/gen_mdl_docs.py` 单向生成。
9. **技能卡瘦身**：口径搬 MDL，方法/平台/红线留卡（metrics-handbook 已瘦身）。
10. **粒度三处钉死**：模型 grain+key；指标 distinct+unit；比率只由已登记指标组合、禁裸写聚合。
11. **query_db 六槽位保留**：QuerySpec 覆盖不了所有场景（探索/复杂查询），六槽位+execute_sql 是自由表达通道。
12. **除零/缺数**：engine 返回 None + `undefined_cells`，agent 如实转述"不适用"，禁止写 0。
13. **评测期望用语义特征组**，不绑字面（G4/S3-3 教训）。

**实现状态**：上述 1~5、7~10、12 已实现并验收（确定性 11/11；跨源转化率/维度拦截走通）；6 部分实现（order_item_id ✓）。

**仍待办：**
- A 组质量债 + B1 回归（见 `rounds/todo-quality-debt.md`；B1 需充值）
- M4：LMDI 乘法贡献引擎 / 恒等式 vs 对账（跨源恒等式只能"声明+校验工具"，不能自算）

## 七、对应四轮框架

- **材料**：MDL 是更强的"材料包"——从"给列名"升级到"给语义模型索引"；
- **Runtime**：`dry_plan` 是新的确定性编排节点，展开过程进 trace；
- **评测**：新增「模型查询」Golden，与既有判分器同构；
- **承载**：本圈不动 UI。
