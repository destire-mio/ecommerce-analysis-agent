"""Public cases contain questions and output schemas, never gold values."""
CATEGORIES=['家电','服饰','家居','美妆','食品']
CAMPAIGN_FIELDS=['gmv_cents','refund_cents','net_cogs_cents','ad_spend_cents','contribution_cents']
PRODUCT_FIELDS=['net_units_28d','available','qualified_inbound','buy_units','purchase_cents']
CASES=[
 {'id':'C1','name':'销售下滑：核对口径后定位损失来源',
  'question':'我是店铺1的经营负责人。比较2026-09-06至09-12与08-30至09-05，核对本周销售额下降是否属实。给出两期支付GMV、变化额、各品类对变化的贡献和损失最大的品类；再结合渠道访客变化判断哪些解释有证据，哪些需要补证。旧看板能否用于这个结论？最后给出下一步经营排查顺序，不能把品类贡献或流量相关性当成因果证明。',
  'schema':{'previous_gmv_cents':'整数','current_gmv_cents':'整数','delta_gmv_cents':'整数','previous_visitor_days':'整数','current_visitor_days':'整数','category_delta_cents':{x:'整数' for x in CATEGORIES},'worst_category':'品类名'},
  'tables':['metric_rules','orders','order_items','fx_daily','products_scd','traffic_daily','etl_lineage'],
  'review':['用真实SQL及规则说明旧看板失效原因，数值差异不能只归咎于数据错误。','渠道变化有分渠道查询证据；访客日数不写成区间去重人数。','区分已算出的品类损失与未证实的转化、价格、流量原因，行动与证据对应。']},
 {'id':'C2','name':'活动复盘：销售额能否转成商品贡献',
  'question':'我是店铺1的活动负责人。复盘2026-08-16至08-31的C01、C02、C03：把截至2026-09-12结算的退款算回原活动，扣除净商品成本及正式广告费。给出每个活动的GMV、退款、净商品成本、广告费、商品贡献和贡献最高活动；建议下一期哪些活动继续、哪些应调整，并指出这些数据是否足以证明活动带来增量利润。优惠券、退款版本及活动结束后的退款需要核对。',
  'schema':{'campaigns':{c:{f:'整数' for f in CAMPAIGN_FIELDS} for c in ['C01','C02','C03']},'best_campaign':'活动ID'},
  'tables':['metric_rules','orders','order_items','refund_events','fx_daily','ad_daily'],
  'review':['广告与退款归属同一活动，区分广告支付窗口和退款观察截止。','未重复扣优惠、重复计退款；退回成本与退款件数一致。','建议引用贡献及成本证据，明确不是全口径利润或因果增量收益。']},
 {'id':'C3','name':'限额补货：销量、库存、在途与采购约束联合决策',
  'question':'我是店铺1的采购负责人。站在2026-09-13 08:00，为商品1到10制定今天的采购方案，采购预算5万元。使用批准的28天净销量、供应商交期+10天覆盖、两仓可售、覆盖期内确认在途、整包装与起订量规则；按近28天净商品毛利顺序分配预算。逐商品给出净销量、可售、合格在途、采购件数和金额；解释有缺口但未采购的商品，说明方案的库存风险。只交付采购建议，不执行下单。',
  'schema':{'products':{str(i):{f:'整数' for f in PRODUCT_FIELDS} for i in range(1,11)},'total_purchase_cents':'整数'},
  'tables':['metric_rules','orders','order_items','refund_events','fx_daily','inventory_snapshots','purchase_orders','supplier_terms'],
  'review':['按仓取最新库存，不加总历史快照；在途以状态及商品覆盖截止筛选。','报告列出覆盖目标、缺口、毛利排序、包装/MOQ、预算分配的证据。','说明未采购源于无需求还是预算受限；不把静态覆盖当成预测最优，未执行采购。']},
 {'id':'C4','name':'复购分析：身份合并、有效订单与观察期联合计算',
  'question':'我是店铺1的会员负责人。比较2026年7月与8月观测历史内首购客户的30日复购率，判断能否据此认定8月用户质量更好。app与web身份应合并，排除员工、测试单、未支付及截至2026-09-12全额退款订单；同日拆单不算复购，观察不满30天的客户单列。给出两个月的成熟客户数、复购客户数、未成熟客户数、复购率，解释分母、首购查找范围与比较局限，形成可供经营会使用的结论。',
  'schema':{'cohorts':{m:{'eligible_customers':'整数','repeat_customers':'整数','immature_customers':'整数','repeat_rate':'0到1小数或null，8位小数'} for m in ['2026-07','2026-08']}},
  'tables':['metric_rules','orders','order_items','refund_events','fx_daily','customer_identity','customer_tags'],
  'review':['先找全部观测历史的有效首购，再切月份；不宣称终身新客。','同日订单不能当复购；全额退款后重求首购，不能只减分子。','8月未成熟客户排除后产生选择差异，不据成熟子集率值证明全月客群更优。']},
 {'id':'C5','name':'跨源对账：从经营GMV解释到财务到账净额',
  'question':'我是店铺1的经营与财务对账负责人。核对2026-09-01至09-12的经营支付GMV与财务到账净额。先确认指标版本，按逻辑流水去重；把本期未结算、前期订单本期到账、商品汇率与舍入差、运费、手续费、退款逐项列成对账桥。逐项给出金额和来源，验证桥的差额为0；指出哪些差异是口径或时点差异，哪些证据缺失才需要财务排查。不能把两个总额相减后随意命名差异，也不能只取首批订单进行对账。',
  'schema':{f:'整数' for f in ['operating_gmv_cents','cash_net_cents','shipping_cash_cents','fees_cash_cents','refunds_cash_cents','prior_goods_cash_cents','current_goods_fx_rounding_cents','current_goods_not_settled_cents']},
  'tables':['metric_rules','orders','order_items','fx_daily','finance_entries','etl_lineage'],
  'review':['每个桥项独立取数且保留订单级跨源匹配证据；差额0不能代替来源验证。','商品支付日/结算日FX与行/订单舍入区分；手续费和退款符号正确。','大结果完整性有记录，真实异常与合理账期差异分开；未改写账本。']}
]

def prompt(case):
 import json
 return case['question']+'\n\n请读取commerce_contract技能及批准的metric_rules，必要时查分区完整性。观察时间固定为2026-09-13，交易数据截至前一天，不随今天日期改变。按业务依赖取数、推理并交付中文报告，每个关键金额/判断关联实际查询(Qn)。在###EVIDENCE###之后加入一个<benchmark_result>JSON</benchmark_result>机器读取块；JSON只填下述结构，金额统一人民币分整数，数量为整数，不填占位文字；不能取得的值填null并解释缺失原因，不猜数。结构：'+json.dumps(case['schema'],ensure_ascii=False)+'\n报告需解释决策和证据边界。无需生成DOCX。'

if __name__=='__main__':
 import json
 from pathlib import Path
 Path(__file__).with_name('cases.json').write_text(json.dumps(CASES,ensure_ascii=False,indent=2))
