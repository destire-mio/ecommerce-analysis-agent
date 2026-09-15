# 电商经营分析 Agent 复杂业务 Benchmark

5条从经营问题到决策报告的端到端任务，共用100万订单、1,554万行构造数据。任务关注多步取数和业务推理：销售下滑诊断、活动贡献复盘、限额补货、复购人群分析、经营与财务对账。

**先读 [5条用例说明](用例说明.md)**，了解每条任务要查哪些数据、为什么需要前一步的结果、怎样判断任务完成。执行情况见 [基线报告](基线报告.md)。

## 交付文件

| 文件 | 用途 |
|---|---|
| [用例说明.md](用例说明.md) | 业务场景、数据关系、逐步任务链、示例答案与验收要求 |
| [contract.md](contract.md) | 对Agent公开的指标口径、去重规则、时间边界与决策规则 |
| [cases.json](cases.json) | 恰好5条机器可读用例，问题、输出结构、所需证据与人工检查项；不含答案 |
| [cases.py](cases.py) | 同一套用例的执行入口与提示词构造，生成cases.json |
| [build.py](build.py) | 随机种子驱动的构造数据生成器，输出公开数据库与私有标准答案 |
| [oracle.py](oracle.py) | 从数据库用SQL重建结果，与生成期的业务事件累计值核对 |
| [platform_mcp.py](platform_mcp.py) | 独立平台原始数据查询服务，提供表目录与只读SQL，不提供用例答案 |
| [run.py](run.py) | 复制未修改的产品Agent入口，在每条用例独立目录运行并保存轨迹 |
| [grade.py](grade.py) | 目标值比对、查询引用和来源覆盖检查；不会把格式正确当业务完成 |
| [audit_data.py](audit_data.py) | 核查跨币种、跨日、分类变更、退款重放和全额退款等陷阱存在，检查优惠金额恒等式 |
| [selfcheck.py](selfcheck.py) | 验收器反例与原始数据适配器检查；不冒充Agent端到端测试 |

本地数据在 `artifacts/scale-1m-v2-seed9142026/`。主库含13张表，平台库含6张表；各表行数、业务日期、文件哈希在 [manifest.json](artifacts/scale-1m-v2-seed9142026/manifest.json)。生成期没有连接现有电商产品库或真实商户系统。

## 环境与复现

在 `/Users/destire/agent-workspace` 执行。生成器和SQL复核器使用Python标准库；真实Agent与MCP使用项目已有 `.venv`，包括mcp、sqlglot、openai。当前执行器只适配现有 `agent_loop.py`，保留其模型、工具门禁、15轮预算与证据格式。它不是一个通用Agent框架。

生成一个新数据集，默认100万订单；输出路径必须不存在：

```bash
python3 benchmarks/commerce_e2e/build.py \
  --out benchmarks/commerce_e2e/artifacts/scale-new-seed \
  --orders 1000000 --seed 9142028

python3 benchmarks/commerce_e2e/oracle.py \
  benchmarks/commerce_e2e/artifacts/scale-new-seed

.venv/bin/python benchmarks/commerce_e2e/selfcheck.py \
  benchmarks/commerce_e2e/artifacts/scale-new-seed
```

运行现有数据上的5条任务。沿用产品配置的 `DEEPSEEK_API_KEY`；执行器缺少凭据时报告BLOCKED，不切换成预置答案。运行真实模型会消耗该账户API额度。

```bash
.venv/bin/python benchmarks/commerce_e2e/run.py \
  benchmarks/commerce_e2e/artifacts/scale-1m-v2-seed9142026 \
  --out benchmarks/commerce_e2e/artifacts/my-baseline \
  --workers 2 --timeout 420
```

可以增加 `--case C3` 只运行一条。`--workers 2` 表示同时执行两个独立用例，用于缩短评测时长，不是并发压测。420秒是本次运行的资源截止，不是业务响应SLA；改变它时应在结果中保留参数，不与旧条件混比。

生成和复核主数据集通常需要数十秒至数分钟，具体受CPU和磁盘影响。数据库文件约1.16 GiB；复核时会创建数据库外的临时中间结果，需要额外磁盘。要扩展订单数，先评估机器资源；本次只实测100万订单级。

## 数据隔离与答案隔离

每条运行目录含未修改的 `agent_loop.py`、公开业务规则、平台MCP适配器，以及指向公开数据库的路径。会话、stdout、stderr、trace、最终报告分开保存。产品现有数据、会话和源代码不参与写入。

`private/expected.json` 与SQL复核结果只由评测器读取，不发送给模型、不注册为工具、不加入运行目录。`用例说明.md`含示例答案，属于评测员材料，不能注入被测Agent。若接入有文件系统工具的另一Agent，应只挂载 `public/` 和 `contract.md`，在另一个进程或容器持有私有答案。

这里的隔离是目录、进程和数据文件分离，共享同一台主机。没有实现操作系统级安全沙箱；没有在商户生产实例上运行；没有生产接口认证、容量或高可用测试。

## 数据和运行结果怎么验收

1. 生成器从逻辑业务事件累计标准答案，再写出带版本、重复记录和跨表关系的数据。
2. `oracle.py`从写好的数据库重建同一业务定义。它不知道生成时的中间累加状态；结果与标准答案逐字段比对。补货预算规则是公开政策，两种计算都执行同一政策。
3. 复核器通过且数据文件哈希一致后，`run.py`才启动被测Agent。标准答案不进入提示词。
4. Agent通过正式CLI调用真实模型、数据库和MCP，输出中文报告、Qn证据与机器指标块。轨迹保留实际查询、工具结果、轮次与模型token记录。
5. 金额按分精确比对，数量按整数比对，比率容差1e-7。还会核对报告引用确实存在、查询成功、所需业务表被引用。
6. 数值和引用检查通过标记REVIEW_REQUIRED。评测员逐条查看 `review_items`，核对查询语义、结果完整性、报告结论和引用对应关系；记录理由后才能标整条PASS。

表名覆盖检查为当前直接SQL工具编写。若后续实现使用已批准的聚合视图或语义层，需要把其血缘映射接入验收器，保持同一业务证据要求；不能仅因少读一张原表而否定等价实现。

### 运行产物

```text
artifacts/<run>/
  run_manifest.json       # 数据、入口、适配器、业务规则、用例哈希与参数
  summary.json            # 已完成执行的用例分项状态
  C1/ ... C5/
    question.txt          # 真正交给模型的问题
    stdout.log
    stderr.log
    agent_result.json     # 正式CLI返回值；没有最终报告时保留错误状态
    answer.md             # Agent最终报告原文
    assessment.json       # 数值错误、引用错误、待人工审查项
    runs/<id>/trace.jsonl # 正式调用链
    sessions/<id>/        # 被测产品保存的会话和证据账本
```

不存在最终报告时，`answer.md`只记录未完成状态；不会把评测器重算的答案替代Agent输出。进程异常、超时或预算耗尽属于本次未完成，不能等价成“不支持所有电商任务”。

## 已做的验证及解释范围

- 主数据集5条标准答案通过SQL重建核对。
- 小数据集、另一个随机种子重放通过SQL重建；5条答案均随种子改变。没有宣称第二个百万订单数据集也已实测。
- 29项评测器/适配器检查覆盖每条用例的单值错误、虚构引用、失败查询引用，另含空结果、缺表、结果超限、只读限制与数字类型。
- 这些检查验证评测材料与基础验收行为；被测Agent是否完成任务，以 [基线报告](基线报告.md) 为准。

指标业务定义是本Benchmark的构造约定；窗口函数与日期运算依据SQLite官方文档：[窗口函数](https://www.sqlite.org/windowfunctions.html)、[日期与时间函数](https://www.sqlite.org/lang_datefunc.html)。金额使用整数人民币分，避免浮点累计误差；历史有效期与日期窗口使用显式边界。

本包不代表原文产品全部功能覆盖；5条用例选取了多步、多源经营分析任务。产品能力与测试数据复杂度是两个问题，不能用数据行数替代端到端结果。
