# 电商经营分析 Benchmark

这个 Benchmark 用 5 条跨表经营任务检验 Agent 能否完成“取数 → 口径判断 → 业务推理 → 带证据报告”的完整链路。数据是构造数据，不是商户实绩；运行产物和大规模数据库默认不进 Git。

## 任务

| 编号 | 问题 | 关键难点 |
| --- | --- | --- |
| C1 | 销售下滑，先排查哪里 | 旧看板口径、历史分类、品类分解、流量证据 |
| C2 | 哪个活动值得继续 | 退款去重与回溯、商品成本、正式广告费 |
| C3 | 预算有限，今天补哪些货 | 净销量、可售库存、合格在途、包装起订量 |
| C4 | 新客复购是否真的更高 | 身份合并、全历史首购、成熟观察期、可比分母 |
| C5 | 经营销售额为何和财务到账不一致 | 流水去重、支付与到账日期、跨源订单匹配 |

## 文件

| 文件 | 用途 |
| --- | --- |
| [`用例说明.md`](用例说明.md) | 业务背景、任务链、示例答案和人工验收条件 |
| [`contract.md`](contract.md) | 对 Agent 公开的指标口径、时间边界和决策规则 |
| [`cases.json`](cases.json) / [`cases.py`](cases.py) | 机器可读用例和提示词构造 |
| [`build.py`](build.py) | 按种子生成公开数据库和私有标准答案 |
| [`oracle.py`](oracle.py) | 用独立 SQL 重建标准答案并核对数据 |
| [`grade.py`](grade.py) | 检查数值、查询引用和来源覆盖 |
| [`platform_mcp.py`](platform_mcp.py) | 平台原始表的只读 MCP 查询服务 |
| [`run.py`](run.py) | 运行真实 Agent 并保存轨迹 |
| [`selfcheck.py`](selfcheck.py) | 检查数据、验收器和反例，不代替端到端运行 |

## 生成和复核数据

```bash
python benchmarks/commerce_e2e/build.py \
  --out benchmarks/commerce_e2e/artifacts/demo \
  --orders 10000 \
  --seed 9142028

python benchmarks/commerce_e2e/oracle.py \
  benchmarks/commerce_e2e/artifacts/demo

python benchmarks/commerce_e2e/selfcheck.py \
  benchmarks/commerce_e2e/artifacts/demo
```

`--out` 必须指向不存在的目录。把 `--orders` 调到 1,000,000 会生成大规模数据，需要预留足够的磁盘和内存。

## 运行 Agent

先在仓库根目录配置 `DEEPSEEK_API_KEY`，再执行：

```bash
python benchmarks/commerce_e2e/run.py \
  benchmarks/commerce_e2e/artifacts/demo \
  --out benchmarks/commerce_e2e/artifacts/my-run \
  --workers 2 \
  --timeout 420
```

运行产物包含每条用例的会话、工具调用、SQL、模型输出和验收结果。它们可能包含较长轨迹，已被 `.gitignore` 排除。标准答案不传给 Agent，评测结论应区分自动检查与人工语义审查。

## 数据隔离

订单库和平台库分开存放。平台 MCP 只提供表目录和只读 SQL，不提供“正确利润”或“正确补货量”接口。标准答案放在本地生成目录的私有区域，只由验收器读取。
