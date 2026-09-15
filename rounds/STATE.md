# 项目状态快照（交接文档）

> 更新: 2026-09-14 ｜ 用途: 清理上下文后的接续起点
> 项目: 电商店铺经营分析助手（NL2SQL agent → 语义层升级中）
> 学习目标: 掌握 agent 设计方法（AGENTS.md 四轮螺旋：材料/Runtime/评测/承载）

## 一、当前位置

```
产品: 电商店铺经营分析助手（大 PRD = bybt-product-spec 825 行, 归纳为 8 场景 S1~S8）
螺旋: 圈1(S1 问数)✓ → 圈2(S2 归因)✓ → 语义层升级圈(P1+M3)✓ → 下一步 M4 或场景
已验证: 确定性验收 11/11 ｜ Golden v2 6/7(G4 校验器字面脆弱,非回归) ｜ S3 4/4
        M3 跨源: 访客数(MCP)/转化率(by date)/维度拦截 全部走通
阻塞: DeepSeek 余额不足(402) —— B1(S3 回归)与 M3 Golden 真跑待充值
```

## 二、已实现（真实运行验证过的）

| 组件 | 文件 | 说明 |
|------|------|------|
| agent 循环 | `agent_loop.py` | 材料包/**语义索引**/九工具(+`query_metrics`)/证据编号Qn/答案校验/会话/预算15 |
| **语义层** | `semantic/{registry,planner,engine}.py` + `data/ecommerce/mdl.yaml` | **QuerySpec 点菜单 → 规划器 → 执行**；本地(sqlite)+平台(platform_mcp)双 provider；指标/比率登记；维度交集校验；key 唯一性加载期拦截 |
| 自店数据 | `data/ecommerce/ecommerce.sqlite` | orders/items(含 `order_item_id` 行级主键)/products，锚点数字: GMV 100万→80万, 订单10000→8000, 单均100, 家电60万→35万 |
| 平台数据(mock) | `data/platform/platform_data.sqlite` | 大盘/渠道流量/漏斗——物理隔离，仅 MCP 可达 |
| mock MCP | `tools/platform_mcp.py` | 3 工具（get_market_overview/get_shop_traffic/get_funnel） |
| 技能卡×3 | `data/ecommerce/skills/` | diagnosis-sop + **metrics-handbook(已瘦身: 口径删,方法留)** + report-sop |
| 评测 | `golden_cases_v2/s3/m3.json` + `tools/run_golden_*.py` + `tools/run_semantic_acceptance.py` | v2 7题 / s3 4题 / m3 跨源4题 / 确定性11条 |
| 记录 | `results/golden_*`、`rounds/roadmap.md`(M1~M7)、`rounds/todo-quality-debt.md`(A/B 待办)、`badcase.md` | |

## 三、关键设计决策（勿回退）

1. **机器管确定性，agent 管理解**——校验/渲染/执行/规划是代码；理解/挑选是 LLM
2. **QuerySpec 点菜单**——agent 报指标名/维度/时间窗，**LLM 不拥有 SUM/COUNT/DISTINCT/JOIN 决定权**；语义 SQL(query_db/execute_sql) 保留为自由表达通道（六槽位覆盖不了所有场景）
3. **知识进卡片，机制进代码**——口径全在 mdl.yaml(唯一权威)；卡只留方法/平台/红线
4. **aggregate-then-align**——比率分子分母各在自己模型聚合，按公共维度对齐后相除；禁止裸 JOIN 事实表
5. **数据物理隔离**——自店库(SQL) vs 平台库(MCP)，但**平台指标以 provider 进语义层**统一点菜
6. **三态**：合法空集(答0)/数据缺失(禁数字)/查询失败(报错)——禁止互相冒充
7. **证据标注**：每个数字引用 (Qn)，###EVIDENCE### 分隔用户视图/审计视图
8. **声明必须被机器验证**——MDL 主键加载期真查库验唯一；ref_sql 输出列必须等于声明列

## 四、运行方式

```bash
source ~/.zshrc   # DEEPSEEK_API_KEY（注意: 当前余额不足）
.venv/bin/python agent_loop.py --question "..." --db ecommerce --bench ecommerce [--session s-xxx]
.venv/bin/python tools/run_semantic_acceptance.py          # 语义层确定性验收(不调 LLM)
.venv/bin/python tools/run_golden_v2.py [--file golden_cases_s3.json | golden_cases_m3.json]
.venv/bin/python tools/gen_mdl_docs.py                     # mdl.yaml → 人读文档(单向生成)
```

## 五、挂账清单（详见 rounds/todo-quality-debt.md）

A 组(质量债)：SQL 注入面收紧 / compare 两期对比 / mcp 多 measure 合并 / MCP 错误结构化 / week-month 粒度
B 组：B1 S3 回归(待充值) / B3+B4 文档同步(进行中) 已在办
后续：M4 LMDI 乘法贡献引擎 → M5 S3~S5 场景 → M6 UI → M7 治理权限

## 六、下一个动作

M4（归因确定性：LMDI/对账）或先还 B 组欠账；B1 需先充值。

