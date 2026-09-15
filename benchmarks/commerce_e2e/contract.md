---
name: commerce_contract
summary: 电商多源数据的业务口径、版本规则、时间边界及预算规则；5条经营任务使用前阅读
---

## 共同口径

分析店铺 shop_id=1。观察日 2026-09-13；交易、广告、访客、账务业务日取到 2026-09-12，库存快照到 2026-09-13 08:00（北京时间）。库外提问日期不改变观察窗口。区间左闭右开。分区完整性以 partition_manifest 为准；缺源、超时不能解释成零。

metric_rules 存批准与废弃版本；按 approved 且生效区间选择。etl_lineage 提供历史报表血缘。schema 只解释字段，不能代替业务定义。查数前应取得这些口径。

orders 每单一行；order_items 每商品行一行；payment_events 每支付状态版本一行；refund_events 每退款状态版本一行。coupon_allocations 每订单每券一行，直接与商品行连接会放大金额。paid_minor 已扣优惠，不能重复减券。gross_minor = paid_minor + discount_minor；金额 minor 为订单原币最小单位。

支付 GMV：仅 paid 且非测试；payment_date_local 是北京时间支付日，pay_time_utc 是 UTC；不含运费、不减退款。按商品行 paid_minor × 支付日 cny_rate_millionths / 1000000 四舍五入到人民币分再求和。非负金额可用 CAST((金额*汇率+500000)/1000000 AS INTEGER)。不要先聚订单再舍入。products_scd 是商品分类历史，匹配 valid_from <= 支付日 < valid_to，不能只取最新分类。legacy_daily_report 使用废弃口径，TOTAL 与 CATEGORY 是重复汇总层级。

退款按 refund_id 取最高 version，再取最高 event_id；只用 settled 且 settled_date < 观察日的版本，先聚到商品行。经营退款按原支付日汇率，退回商品成本 = refund_quantity × 原商品行 unit_cost_cny_cents。当前合成数据每行最多一个逻辑退款；状态版本和重放不是多笔退款。支付状态是否成功由 orders.status 权威确定，payment_events 用于核对，不逐事件计成交。

## 跨源数据

订单库用 execute_sql；平台库通过 list_platform_tables/query_platform 访问。SQL 方言 SQLite。连接键 order_id/product_id/campaign_id 应保留；分源先聚合同粒度再合并。大结果按键分页或由支持中间结果的实现分批合并，不能将首批200行当总体。当前被测 Agent 没有暂存跨源结果的工具；评测应记录这个能力缺口。

平台 traffic_daily：每日×渠道有版本，只用 is_final=1。visitor_days 是按互斥渠道归属的访客日数，同一客户跨日可重复；窗口求和不代表窗口去重 UV。paid_user_days 同属访客日口径，不等于订单数。

ad_daily：店铺×日×活动的 estimate 与 final 并存，只计 final。库存按店铺×仓库×商品×时间，必须每仓取截止时点最新记录后求和。supplier_terms 每商品一行；purchase_orders 每采购单一行；finance_entries 同一 journal_id 有旧版本，应按 version DESC,event_id DESC 去重，仅 posted。

## 活动复盘

活动商品贡献 = 优惠后支付 GMV − 已结算退款 −（原商品成本 − 退回商品成本）− 同活动支付窗口 final 广告费。退款可以发生在活动结束后，截到观察日。不含平台费、物流、人力；这不是增量利润，也不证明活动的因果效果。best_campaign 按上述贡献金额最大选择，平局按活动ID升序。可以据此提出继续或暂停审查建议，不能声称已执行投放。

## 补货规则

候选 product_id=1..10。近28天为 [2026-08-16,2026-09-13)，净销量扣截至观察日已结算退货。每仓最新快照可售 = on_hand − reserved − damaged。覆盖天数 = lead_days + 10。计入 eta <= 2026-09-13 + 覆盖天数的 confirmed 在途，排除 cancelled 与晚到货。目标数量 = ceil(max(0,近28天净销量) × 覆盖天数 / 28)。缺口 = max(0,目标 − 可售 − 合格在途)；缺口大于0时，按整包向上取整并达到 min_order。

采购预算5000000分。候选按近28天净商品毛利降序，毛利 = 优惠后GMV − 已结算退款 − 净商品成本；平局按商品ID升序。依次分配预算：取建议量与剩余预算可购买整包量的较小值；不足最小起订量则不买并处理下一商品。不承诺该静态覆盖策略是全局最优策略。

## 复购规则

用 customer_identity.canonical_id 合并 app/web；排除订单日处于 employee 标签有效期的客户订单、测试单、未支付单、截至观察日商品净额<=0的订单。先在全部已提供历史中求首购，再分月；本评测的“新客”指180天观测历史内首购，数据不能证明终身新客。

30日复购：首购日之后、首购日+30天之前有另一有效订单；同日拆单不计复购。first_date+30 <= 2026-09-13 才进入成熟分母，其余计 immature_customers。repeat_rate = repeat_customers / eligible_customers，保留8位小数；分母为0输出 null，不输出0%。比较2026-07与2026-08，并说明8月后半段尚未成熟。

## 经营与财务对账

经营 GMV 按支付日归属。finance_entries 按 posted_date 归属、结算日汇率；goods/shipping 为正，fees/refunds_日期 为负。对账窗口 [2026-09-01,2026-09-13)。先按 journal_id 版本去重，再汇总。

现金净额 = 本期经营GMV − 本期已支付未结算商品款 + 前期支付本期结算商品款 + 本期商品汇率与舍入差 + 本期到账运费 + 本期手续费负数 + 本期退款负数。商品汇率与舍入差需按 order_id 匹配本期支付且本期结算的订单，实际商品到账减原支付GMV；分源大结果需要完整处理。不能通过调整GMV口径强行抹平差异。
