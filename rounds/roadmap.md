# 路线图：从语义层到完整产品（v2 · 交接版）

> 更新：2026-09-14 晚 ｜ 给后续 agent 使用：**按 M 编号顺序执行，每步做完必须跑"验收"栏的命令并贴证据**
> 原则：一次一个用户可见路径；真实运行 + 证据 + 回归；机器管准确性，agent 管理解。
> 环境：`source ~/.zshrc`（DEEPSEEK_API_KEY）；必须用 `.venv/bin/python`（系统 python 没有 mcp/sqlglot/pyyaml）。

## 0. 当前位置（已完成，勿重做）

| 里程碑 | 状态 | 证据 |
|---|---|---|
| P1 语义层端到端（mdl.yaml + registry/planner/engine + query_metrics 工具） | ✅ | 确定性验收 11/11；Golden v2 7/7；S3 4/4 |
| M2 数据底座（`order_item_id` 行级主键 + MDL 主键加载期唯一性拦截） | ✅ | 重建库锚点数字不变；假 key 试炸成功 |
| M3 跨源（fct_orders/shop_traffic 模型、MCP provider、aggregate-then-align、维度交集校验、引导+拦截） | ✅ | 转化率 by date 7行正确；访客按品类被拦；M3 Golden 4/4 |
| M3.5 错误健壮性（A3：骨架对齐/不适用成因/provider 错误细分） | ✅ | 三态四场景正确；11/11 不回归 |
| M1 语义层收口（gen_mdl_docs 生成文档、description 必填、技能卡瘦身、除零规范） | ✅ | S3 回归 4/4（瘦身无回归） |
| 中力度拦截（已登记 mcp_tool 裸调→use_query_metrics 引导错误） | ✅ | M3-2/M3-4 行为由裸调转点菜 |

**当前欠账（低优先级，用户拍板拉低）**：`rounds/todo-quality-debt.md` A 组
（A1 注入面收紧——**真上生产前必须还**；A2 compare 两期对比；A2b mcp 多 measure 合并；A4 week/month 粒度）。

---

## 1. 剩余里程碑

### M4 · 归因能力（S2 增强）—— 下一个
- **目标**："为什么下降"从 LLM 手算 → 机器确定性计算。
- **交付**（见 `rounds/M4-实现文档.md`）
  1. **LMDI 乘法贡献引擎**：`semantic/formulas.py` 纯函数，并入 calculate 公式 `lmdi`
  2. **恒等式对账**：mdl.yaml 声明 identities/reconciliations，LMDI 算完自动验（2% 容差，警告不拒绝）
- **取消**：归因诊断树（M4.5）——"钻哪一层"是**判断**，机器硬编码是错的；判断留 agent。
- **验收**：确定性 11/11 不回归；`tools/run_lmdi_acceptance.py`（贡献 `+99502.43/-299502.43/0`、闭合<1e-3）；golden ≥3 题
- **预估**：一天

### M4-5 · A2 compare 两期对比（升优先级，与 M4 并列）
- **目标**：`query_metrics` 支持 `compare:{start,end}`，一次给 curr/prev/delta/pct（机器算，除零→不适用）。
- **交付**（见 `rounds/A2-compare-实现文档.md`）：planner 建两期节点+派生 4 列；engine 复用对齐；tool schema/材料包加 compare。
- **理由**：归因下钻每层都要"比两期"；没有它 agent 查两次自己减。
- **验收**：`tools/run_compare_acceptance.py`（家电 -25万/-41.67%、其他 +5万/+12.5%、访客 +11621/+11.74%）；golden ≥2 题
- **预估**：半天

### M5 · 场景 S4 图表 / S5 报告
- **S3 口径解释 + 口径治理**：**已单独定案** → `rounds/S3-口径治理-实现文档.md`（可执行；读+提+审批闭环）
- **S4/S5**：见 `rounds/M5-实现文档.md`（**草案，待讨论**）
  - S4 图表：倾向输出 Vega-Lite 风格 JSON（不引 matplotlib，前端渲染）；待定
  - S5 报告：口径说明自动从 MDL 生成；待定
- **预估**：S3 一天；S4/S5 一天

### M6 · 产品承载（第 4 轮）
- **交付**（见 `rounds/M6-M7-范围与待决策.md`）：最小 UI（HTTP 服务 + 单页，提问→结论+查看依据）｜冒烟脚本｜发布门禁
- **硬约束**：本轮不新增业务功能；Runtime 即 `agent_loop.run_agent`
- **预估**：一天~一天半

### M7 · 治理与运维（S6~S8，可后置）
- **交付**（见 `rounds/M6-M7-范围与待决策.md`）：S8 租户注入(改 shop_id 硬编码)/列级权限/审计；S7 血缘/指标版本；S6 成本预算
- **预估**：一两天（最小版=租户注入+审计）
- **注意**：M6/M7 **设计未讨论**，先讨论再写任务书

---

## 2. 横切事项（穿插做，不单独成天）

- 指标**同义词**（"销售额"→GMV）：mdl.yaml 加 `synonyms`，点菜单接受同义词
- schema 检索（按问题取相关模型，替整包 dump）——大库时才显价值
- 会话条件结构化（条件卡）、上下文压缩
- 测试反例补全（"不该能算的必须报错"）

---

## 3. 执行纪律（给后续 agent）

1. **先读**：`rounds/STATE.md`（现状快照）→ 本文件对应 M 的"交付+验收" → `AGENTS.md`（方法铁律）→ `rounds/todo-quality-debt.md`（欠账）
2. **每步**：实现 → 跑验收命令 → 贴证据 → **回归**（确定性 11/11 + 既有 golden 不掉）→ 失败回流
3. **别碰**：已完成里程碑的代码（除非验收失败回流）；锚点数字（GMV 800000/1000000、订单 8000/10000、单均 100）变了吗 = 你改错了
4. **评测期望**用语义特征组，不绑字面（G4/S3-3 教训）
5. LLM 余额不足(402)时：确定性验收照跑，LLM 类回归挂起并在文档标注

---

## 4. 完整产品完成的定义

- [ ] M4~M6 全绿（M7 可后置为最小权限+审计）
- [ ] S1~S5 场景 golden 全 PASS
- [ ] UI 端到端冒烟 + 打包通过
- [ ] A1 注入面已还（生产门槛）
- [ ] 全套 Golden（v2/s3/m3/m4/m5）+ 确定性验收全绿
