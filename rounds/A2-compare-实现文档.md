# A2 实现文档（任务书）：query_metrics 支持 compare 两期对比

> 写给执行 agent：**零上下文可执行**。先读 `rounds/STATE.md` → `AGENTS.md` → 本文。
> 环境：`source ~/.zshrc` ｜ **必须** `.venv/bin/python` ｜ 工作目录 `/Users/destire/agent-workspace`
> 前置：M3 语义层已完成（`semantic/{registry,planner,engine}.py` + `query_metrics`）。

---

## 0. 为什么做

归因下钻**每一层都要"比两期"**（如按品类看 GMV 近7天 vs 前7天）。现在 `query_metrics` **一次只查一个时间窗**，agent 要对比就得查两次、自己对齐相减——麻烦、易错，且回退到手写 SQL。

**目标**：`query_metrics` 支持 `compare`，**一次调用给出两期 + 差额 + 降幅**（机器算，除零标"不适用"）。

---

## 1. 设计定案（已讨论锁定）

| 点 | 定案 |
|---|---|
| 对比期指定 | **显式** `compare: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`（半开区间，与 time_range 同规则）。**不做** `previous_period` 自动推 |
| 语义 | `time_range` = **本期(curr)**；`compare` = **基期(prev)** |
| 输出 | **甲**：每个指标出 4 列 `<指标>`(curr) / `<指标>_prev` / `<指标>_delta` / `<指标>_pct` |
| 差额/降幅 | **机器算**：delta=curr-prev；pct=(curr-prev)/prev*100；**prev=0 → pct 为 None（"不适用"）**，delta 照常给 |
| 与 LMDI | 独立：compare=单指标两期；lmdi=多因素乘法分解 |
| 限制 | `compare` 与 `time_grain="day"` **不可同时用**（两期日期不同无法按天对齐）→ 报 `PlanError("compare_with_grain")` |

---

## 2. QuerySpec 变化

```json
{
  "metrics": ["GMV"],
  "dimensions": ["category"],
  "time_range": {"start": "2026-09-06", "end": "2026-09-13"},   // curr
  "compare":    {"start": "2026-08-30", "end": "2026-09-06"}    // prev（新增, 可选）
}
```

---

## 3. 实现步骤

### 步骤 1 · planner（`semantic/planner.py`）

- `plan(spec)` 里检测 `spec.get("compare")`：
  1. 校验：有 compare 且 `time_grain=="day"` → `PlanError("compare_with_grain", ...)`
  2. **基期节点**：用同一套 metric/measures/dimensions，但时间窗换成 compare，**measure 别名加 `_prev` 后缀**（如 `m_0_prev`）
  3. **本期节点**：别名不变（`m_0`）
  4. `align_on` = dimensions（**不含 period**，两期值要落同一行）
  5. `derived` 扩展为每个指标 4 项：
     - `{name: M,       expr: 本期表达式}`
     - `{name: M_prev,  expr: 把本期表达式里的 m_i 全换成 m_i_prev}`
     - `{name: M_delta, expr: (M) - (M_prev)}`
     - `{name: M_pct,   expr: ((M) - (M_prev)) / (M_prev) * 100}`
- `AggNode` 已有 `provider/model/sql/mcp_call/...`，本期/基期分属不同节点即可，无需新字段（节点按 (provider, model, 期) 分组）。

### 步骤 2 · engine（`semantic/engine.py`）

- 无需结构性改动：`_align` 按 dimensions 对齐会自然把本期/基期值填进同一行（键相同）。
- 派生求值 `_safe_eval`：pct 的除零已返回 `None`（现有 reason 机制），delta 正常。
- 输出列顺序：`dimensions + [M, M_prev, M_delta, M_pct]`（保持稳定）。
- `undefined_*` / 三态逻辑沿用现有。

### 步骤 3 · 材料包 / 工具描述（`agent_loop.py`）

- `query_metrics` 的 tool schema 参数里加 `compare`：
  ```
  "compare": {"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"}},
              "description":"可选: 基期时间窗(半开区间), 与 time_range 对比。给出后每个指标返回 curr/prev/delta/pct"}
  ```
- 材料包（`build_material`）的引导语补一句：
  “需要两期对比时，在 query_metrics 里加 compare:{start,end}，直接得到差额/降幅，不要自己查两次相减。”

### 步骤 4 · mdl.yaml / registry

- **无需改**（compare 是请求态，不是声明态）。

---

## 4. 验收（跑命令，贴证据）

### 4.1 不回归（必须）
```bash
.venv/bin/python tools/run_semantic_acceptance.py     # 期望 11/11
```

### 4.2 新增确定性验收 `tools/run_compare_acceptance.py`（新建）

真数断言（负7天 vs 前7天，与 G2/G3 一致）：

| 用例 | 断言 |
|---|---|
| GMV 总量 | curr=800000, prev=1000000, delta=-200000, pct≈-20.0 |
| GMV 按品类 | 家电: curr=350000, prev=600000, delta=-250000, pct≈-41.6667<br>其他行业: curr=450000, prev=400000, delta=+50000, pct≈12.5 |
| 跨源 访客数 | curr=110596, prev=98975, delta=+11621, pct≈11.7413（走 MCP provider） |
| 除零 | 构造 prev=0 的单指标（如某过滤条件）→ pct 为 None, delta 为数值 |
| 冲突 | compare + time_grain=day → PlanError("compare_with_grain") |

浮点断言用容差 `abs(...) < 0.01`（金额）/ `abs(...) < 0.01`（百分点）。

### 4.3 agent 真跑（可选，需余额）`data/ecommerce/golden_cases_a2.json`（新建 ≥2 题）
- “近7天各品类 GMV 比前7天变化多少？” → 必须走 query_metrics(含 compare)，答案含 家电 -25万 / 其他 +5万
- 判定：数字正确 + 调用工具含 query_metrics

---

## 5. 禁改清单

- 锚点数字：GMV 1000000/800000、订单 10000/8000、单均 100、访客 98975/110596、家电 600000/350000、其他 400000/450000
- 现有 `query_metrics`（无 compare 时）的行为/输出**完全不变**（compare 是纯增量）
- M3 拦截逻辑、LMDI 相关（尚未实现）不动
- 评测期望用语义特征组，不绑字面

## 6. 交回时报告

1. 改动文件 diff 摘要
2. §4.1/4.2 命令**真实输出**
3. 问题与处置
4. 未完成项（如实）
