# 下一圈（待实现）：MDL 语义层 + dry_plan（NL→MDL→SQL）

> 日期：2026-09-14 ｜ 状态：设计完成，明天开工
> 来源：大淘宝技术《淘宝百亿补贴数据分析助手 Agent 实战》（NL2MDL2SQL 路径）
> 参考：WrenAI 文档 https://docs.getwren.ai/oss/concepts/what_is_mdl ｜ 仓库 https://github.com/Canner/WrenAI ｜ SQLGlot https://github.com/tobymao/sqlglot
> 依赖：已装 `pyyaml`、`sqlglot`（在 .venv）

---

## 0. 一句话心智模型（先看这个）

**MDL = 一张“名字 → 事先写好的 SQL”的对照表。**

```
名字: 支付金额  →  SQL: SELECT SUM(amount) ... WHERE status='paid' ...
名字: 支付订单数 →  SQL: SELECT COUNT(DISTINCT order_id) ... WHERE ...
```

- 以前：用户问“支付金额”，agent **自己现写**那段 SQL（可能写错、漏条件）
- 之后：agent 只说“支付金额”这个名字，**由引擎查出写好的那段 SQL** 去执行

三步示例（对实际数据）：

**第 1 步 · 定义**（名字 = 一段 SQL，存进文件）
```sql
SELECT i.amount AS gmv_amt, i.category, i.product_id, o.pay_time
FROM order_items i JOIN orders o ON o.order_id = i.order_id
WHERE o.shop_id = 1 AND o.status = 'paid'
```
给它起名 `paid_gmv`，写进 `mdl.yaml`。

**第 2 步 · 使用**（agent 只写名字）
```sql
SELECT SUM(gmv_amt) FROM paid_gmv
WHERE pay_time >= '2026-09-06' AND pay_time < '2026-09-13'
```

**第 3 步 · 展开**（引擎换回真 SQL）
```sql
SELECT SUM(gmv_amt) FROM (
  SELECT i.amount AS gmv_amt, i.category, i.product_id, o.pay_time
  FROM order_items i JOIN orders o ON o.order_id = i.order_id
  WHERE o.shop_id = 1 AND o.status = 'paid'
) AS paid_gmv
WHERE pay_time >= '2026-09-06' AND pay_time < '2026-09-13'
```

本质就是数据库里早有的**视图（view）**：“一段 SQL 存起来、起个名、以后当表查”。MDL 只是把它写进文件、加上字段说明、关系、口径。

---

## 1. 为什么要做（现状痛点）

现在 agent 直接对着原始表写 SQL，表名/列名/JOIN/口径过滤全靠它自己拼。后果：
- **口径靠 LLM 自觉**：`status='paid'`、`shop_id=1` 这类必加条件写在技能卡里，靠它记
- **不沉淀**：`approved_sql.json` 是死的、且已漂移（品类名/订单数/单均三处）
- **整包 dump schema**：没有“按问题取相关模型”的检索
- **JOIN 手拼**：关系没声明，靠 agent 自己写

MDL 把“每次都可能错的口径/结构”变成“写死一次”，把 agent 的犯错面从**整条 SQL** 缩到**几个参数**（时间窗、分组、筛选项）。

---

## 2. MDL 是什么（对象表 + 执行机制）

物理 schema 描述“怎么存”，MDL 描述“是什么意思”。一组 YAML 编译成 manifest 给引擎用。

| 对象 | 是什么 |
|---|---|
| **Model** | 逻辑数据集：物理表（`table_reference`）或一段 SQL（`ref_sql`） |
| **Column** | 暴露字段：可改名（`expression` 映射）、可计算（`is_calculated+expression`）、主键 |
| **Relationship** | 可复用的 join 逻辑（`join_type` + 等值条件） |
| **View / Cube** | 稳定虚拟表 / 预聚合对象（measures/dimensions/hierarchies） |
| **knowledge/** | 业务规则（`rules/`）+ 已确认 NL→SQL 对（`sql/`） |

**执行机制**（不是提示词，是执行期展开）：
```
对着模型写的 SQL
  → sqlglot 解析 + 限定表/列
  → 找到引用的 MDL 对象
  → 语义引擎展开（model / 关系 / 计算字段 / view）
  → 策略检查 → 方言转译
  → 可执行 SQL
```

---

## 3. 本场景要的 MDL（自店库）

库实际字段：
- `orders(order_id, shop_id, pay_time, status)`
- `order_items(order_id, product_id, category, amount)`
- `products(product_id, product_name, category)`

| 模型 | 粒度 | 固化了什么（agent 不用管） | 暴露字段 | 服务场景 |
|---|---|---|---|---|
| **`paid_gmv`**<br>商品支付金额 | 订单明细行 | JOIN `order_items`+`orders`；`shop_id=1`；`status='paid'`；不抵减退款 | order_id, product_id, category, **gmv_amt**(=amount), pay_time | S1 问数、S2 品类/商品拆解、S5 报告 |
| **`paid_orders`**<br>支付订单数 | 订单 | `shop_id=1`；`status='paid'` | order_id, pay_time, status | 支付订单数；单均分母 |
| **`products`**<br>商品维表 | 商品 | — | product_id, product_name, category | 商品名、Top 商品 |

**关系**：`paid_gmv.product_id → products.product_id`（MANY_TO_ONE）

**平台侧 3 张**（market_overview / shop_traffic / funnel）**不进 MDL**——只能走 MCP 工具，SQL 语义层够不到；口径说明留在手册卡。

`mdl.yaml` 草稿：
```yaml
schema_version: 1
models:
  - name: orders
    table: orders
    primary_key: order_id
    columns: [order_id, shop_id, pay_time, status]
  - name: order_items
    table: order_items
    columns: [order_id, product_id, category, amount]
    relationships:
      - name: items_orders
        to: orders
        join_type: MANY_TO_ONE
        condition: order_items.order_id = orders.order_id
  - name: products
    table: products
    primary_key: product_id
    columns: [product_id, product_name, category]
  - name: paid_gmv
    description: 商品支付金额(批准口径)——order_items.amount 求和, 仅 paid, 不抵减退款
    ref_sql: |
      SELECT i.order_id, i.product_id, i.category, i.amount AS gmv_amt, o.pay_time
      FROM order_items i JOIN orders o ON o.order_id = i.order_id
      WHERE o.shop_id = 1 AND o.status = 'paid'
    columns: [order_id, product_id, category, gmv_amt, pay_time]
  - name: paid_orders
    description: 支付订单数(批准口径)——仅 paid 订单
    ref_sql: |
      SELECT o.order_id, o.pay_time, o.status
      FROM orders o WHERE o.shop_id = 1 AND o.status = 'paid'
    columns: [order_id, pay_time, status]
metrics:
  - {name: paid_gmv_item, model: paid_gmv, expression: SUM(gmv_amt), unit: 元}
  - {name: paid_orders,   model: paid_orders, expression: COUNT(DISTINCT order_id), unit: 单}
```

---

## 4. 两条落地路

| | 路 A：接 WrenAI | 路 B：手搓极简 MDL（建议） |
|---|---|---|
| 做法 | pip 装 wrenai，写 YAML，用 CLI/SDK | 自己写 `mdl.yaml` + `dry_plan` 展开器 |
| 代价 | 重依赖（Rust core / lancedb / embedding）；**不支持 SQLite**，要转 DuckDB | 要手写展开器，但可控、能内化概念 |
| 适合 | 要工业效果、不介意黑盒 | 学习项目、想搞懂机制 |

---

## 5. 缺口对照（我们 vs 文章那套）

| 能力 | Wren/文章 | 我们现在 |
|---|---|---|
| MDL 声明式语义模型 | ✅ | ❌ 只有散的 `metrics.yaml` + 自动 dump schema |
| 模型展开规划器 | ✅ sqlglot+CTE+wren-core | ❌ agent 直接写原始 SQL 手拼 JOIN |
| 值画像 | ✅ | ✅ peek_values / 值索引 / 画像 |
| 歧义检测 | ✅ | ✅ ask_user |
| 生成轨迹 | ✅ dry-plan | ✅ 账本 Qn + trace.jsonl |
| 重试修复 | ✅ | ✅ 错误路标 + 重试 |
| 评测 | ✅ 6D 加权 | ⚠️ 有 golden，仅 PASS/FAIL |
| 指标公式体系 | ✅ 乘法/加法/比率 + 恒等式 | ⚠️ `metrics.yaml` 有公式，无 formula_type/恒等式校验 |
| 归因诊断树引擎 | ✅ 指标注册表驱动 | ❌ 靠卡片 + LLM 自主拆 |
| Schema 上下文检索 | ✅ memory fetch（向量） | ❌ 整包 dump |
| NL→SQL 沉淀复用 | ✅ memory recall/store | ⚠️ `approved_sql.json` 死的、已漂移 |
| 列级权限 / 多方言 | ✅ | ❌（S8/后续） |

---

## 6. 完整实现要考虑的 7 层

**一、MDL 文件本身**：YAML 源文件 → 编译产物 manifest（产物不入库）；`schema_version` 迁移；模型两种来源（表/ref_sql）；列定义（改名/计算/主键/类型/**粒度声明**）；列级暴露（权限）。

**二、关系与计算字段**：join_type + 等值条件；**TO_MANY 遍历必须聚合防行放大**；公式权威只能一个来源（已踩过漂移）；递归展开要有环检测。

**三、指标/立方体**：公式类型（乘法/加法/比率）+ **乘法恒等式铁律**；cube（measures/dimensions/time_dimensions/hierarchies）；时间语义与零值/负值/分母 0 处理。

**四、规划器（dry-plan）——核心难点**：sqlglot 解析+限定；只加载相关模型切片；展开（模型→子查询/CTE、计算字段、关系注入 join、视图、递归）；三档接口（dry_plan / dry_run / query）；结构化错误；手写展开器 vs wren-core 是最大工程量分歧。

**五、上下文供给**：schema linking（问题→相关模型/列，决定上限）；关键词→向量检索；上下文预算；歧义检测。

**六、知识层与治理闭环**：rules（我们的技能卡）；NL→SQL 对沉淀/召回；采集→沉淀→审核→复用；schema 变更→重生成；血缘影响分析。

**七、执行/安全/评测**：连接器（方言/类型/dry-run/只读/超时）；列级权限与审计；展开正确性+结果正确性回归；生成轨迹（模型 SQL + 展开 SQL 进账本）。

---

## 7. 分圈落地路线

| 圈 | 做什么 | 难度 |
|---|---|---|
| **1** | 3 模型 + 1 关系 + 手写 `dry_plan` 展开器，跑通一条链路 | 小 |
| 2 | 计算字段 + 指标恒等式校验 | 中 |
| 3 | 上下文检索（按问题取相关模型，替掉整包 dump） | 中 |
| 4 | NL→SQL 沉淀/召回（memory） | 中 |
| 5 | 关系自动遍历 + cube/下钻 | 大 |
| 6 | 治理闭环（schema 变更重生成、血缘、权限） | 大 |

**已有不用重造**：值画像、歧义反问、生成轨迹、重试修复、golden 评测、只读门禁、计算器、报告对账。

---

## 8. 第 1 圈最小切片（明天开工）

1. 写 `data/ecommerce/mdl.yaml`（上面第 3 节草稿）
2. 新工具 `dry_plan(modeled_sql)`：sqlglot 解析 → 把模型名展开成子查询/CTE → 返回**展开后的真实 SQL**（不执行）；同时进账本（生成轨迹）
3. 材料包里加“可用语义模型”索引，引导 agent 用模型名
4. **验收**：一条“按模型名查 GMV”的链路——`dry_plan` 输出可读且正确的真实 SQL，执行结果 = 800,000

## 9. 验收与回归

- 现有 Golden Cases（v2 七题 + S3 四题）**必须不回归**
- 新增 Golden：模型查询 → 展开 SQL 正确 + 结果正确

## 10. 未决 / 风险

- 展开器手写 vs 尽量交给 sqlglot（sqlglot 能 parse SQLite，但 MDL 语义展开要自己写）
- memory（NL→SQL 召回/沉淀）——先不做，等 MDL 跑通
- 归因诊断树引擎（文章第七章）——更后面单独一圈
- 若走路 A：演示库 `ecommerce.sqlite` 需转 DuckDB
- `render_intent`/`query_db`（六槽位）与模型层如何融合——待定
