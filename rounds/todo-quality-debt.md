# 待办清单（A 质量债 / B 欠账）

> 建：2026-09-14 ｜ 状态：**A2 已升级（并 M4 做）；其余 A 组优先级调低（用户拍板）；B 组已办结**
> 来源：M3 完成后的全盘盘点。A 组欠着（真上生产前必须还 A1），C 组按路线图推进。

## A. 做了但有隐患（质量债）—— 优先级已拉低，先欠着

- [ ] **A1 · SQL 注入面收紧**（原高→**低**）
  位置：`semantic/planner.py::_where`
  问题：filters 的 value 用 f-string 直接拼 SQL，agent 可传恶意字符串。
  兜底现状：应用层只读门禁(gate_check) + `PRAGMA query_only` 双层挡着，实际风险可控。
  方案（已定，未实现）：白名单校验——op 只允许 `= != > < >= <=`；
  数字 value 须匹配 `^-?\d+(\.\d+)?$`；字符串 value 禁止 `' " ; -- /* */ \`；
  dimension 必须在模型声明的 dimensions 里。
- [~] **A2 · compare 字段实现**（**已升优先级** → 见 `rounds/A2-compare-实现文档.md`，与 M4 并列做）
  定案：显式 `compare:{start,end}`（不做 previous_period）；输出 curr/prev/delta/pct（机器算，除零→不适用）；
  compare 与 time_grain=day 不可同时用。理由：归因下钻每层都要比两期。
- [ ] **A2b · mcp 模型多 measure 合并单节点**（原中→**低**）
  现在 mcp 模型每个 measure 建一个节点 = 调多次工具（浪费）。合并成一个节点，
  一次调用出多列。
- [x] **A3 · MCP 错误结构化 + 跨源缺行对齐策略** ✅ 2026-09-14 完成
  ① `_align` 重写两遍法：键并集骨架 + 缺行留 None（不依赖节点顺序，反向缺数也不丢行）；
  ② `_safe_eval` 返回 (value, reason)：zero_division（真不适用）vs missing_operand（缺数）；
  输出带 undefined_reasons + 人话 undefined_note；③ `_run_mcp` 错误细分：
  provider_no_data（合法缺数）vs provider_error（工具故障）。三态判定校准：
  空行=empty / 有行全不适用=no_data / 有值=ok。
- [ ] **A4 · time_grain 支持 week/month**（低）
  现在只有 total/day。

## B. 该做没做（已办结）

- [x] **B1 · 技能卡瘦身后跑 S3 回归** ✅ 4/4 PASS（口径题靠材料包语义索引答对）
- [x] **B2 · M3 Golden 用例** ✅ `golden_cases_m3.json` 4/4 PASS
  （引导+拦截后 agent 走 query_metrics 点菜；M3-2 期望从 110596 改为渠道数 56057/27212）
- [x] **B3 · 文档同步** ✅ STATE.md（语义层圈状态+402解除）、AGENTS.md 语义层章节、
  `语义层说明.md` 重新生成
- [x] **B4 · AGENTS.md 决策记录更新** ✅ QuerySpec 路线/比率放宽/provider 进语义层/
  机器验证声明/六槽位保留（13 条已定）
- [x] **B5 · query_db 六槽位保留** ✅（用户拍板：必须存在，覆盖不了所有场景）
- [x] **B6 · 中力度拦截落地** ✅ 已登记 mcp_tool 的平台工具裸调 → 结构化
  `use_query_metrics` 错误+指引；材料包引导语同步强化；M3-2/M3-4 行为由裸调转为点菜

## C. 里程碑任务书（均已就绪，按序执行）

```
A2 → M4 → S3 → S4/S5 → M6 → M7
对应任务书: rounds/{A2-compare, M4, S3-口径治理, M5-S4S5, M6, M7}-实现文档.md
```
横切（穿插）：指标同义词、schema 检索、会话条件结构化、上下文压缩。

## C2. M7 后续治理/运维（最小版之外，不急）
- [ ] 血缘图（metric→measure→model→物理表）
- [ ] 指标 temporal / 多版本查询（按历史日期用对应口径）
- [ ] 变更影响分析（改口径影响哪些指标/报表）
- [ ] 成本预算门禁（每会话 token 上限）
- [ ] 缓存/预聚合（评审也说按需）
- [ ] 真多租户数据 + 端到端隔离测试

## D. 报告/图表 后续优化（S5 窄版之外，不急）

- [ ] 报告**图表嵌入**：把 S4 的 ECharts 图渲染成图片嵌进 docx
- [ ] 报告**模板/美化**：封面、目录、章节样式
- [ ] 报告**来源追溯**：每个 Qn 旁标注"用了哪个模型/口径版本"
- [ ] **多格式导出**：docx 之外支持 PDF/Markdown
- [ ] 报告**段落可点**（UI 里点数字跳到底层 SQL/证据）
- [ ] temporal 查询（按历史日期用对应口径版本）、血缘/影响分析（归 M7）
