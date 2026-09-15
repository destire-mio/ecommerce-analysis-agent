"""Seeded, standalone business-data workload. Never touches the existing product data."""
from pathlib import Path
from datetime import date, timedelta, datetime
from collections import defaultdict
import argparse, sqlite3, json, random, time, hashlib, math

START=date(2026,3,17); CUT=date(2026,9,13)
CATS=['家电','服饰','家居','美妆','食品']
def iso(d):return d.isoformat()
def cents(n,rate):return (n*rate+500000)//1000000
def ratio(n,d):return None if not d else round(n/d,8)
def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def connect(p):
 c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=OFF');c.execute('PRAGMA synchronous=OFF');c.execute('PRAGMA cache_size=-32000');return c

SCHEMA='''
CREATE TABLE orders(order_id INTEGER PRIMARY KEY,shop_id INTEGER,customer_ref TEXT,pay_time_utc TEXT,payment_date_local TEXT,status TEXT,currency TEXT,is_test INTEGER,campaign_id TEXT,channel TEXT,shipping_cny_cents INTEGER);
CREATE TABLE order_items(item_id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,quantity INTEGER,gross_minor INTEGER,discount_minor INTEGER,paid_minor INTEGER,unit_cost_cny_cents INTEGER);
CREATE TABLE payment_events(event_id INTEGER PRIMARY KEY,payment_id TEXT,order_id INTEGER,version INTEGER,state TEXT,goods_minor INTEGER,recorded_date TEXT);
CREATE TABLE refund_events(event_id INTEGER PRIMARY KEY,refund_id TEXT,item_id INTEGER,version INTEGER,state TEXT,refund_quantity INTEGER,refund_minor INTEGER,settled_date TEXT);
CREATE TABLE products_scd(product_id INTEGER,version INTEGER,product_name TEXT,category TEXT,valid_from TEXT,valid_to TEXT,PRIMARY KEY(product_id,version));
CREATE TABLE customer_identity(customer_ref TEXT PRIMARY KEY,canonical_id INTEGER);
CREATE TABLE customer_tags(canonical_id INTEGER,tag TEXT,valid_from TEXT,valid_to TEXT);
CREATE TABLE coupon_allocations(order_id INTEGER,coupon_id TEXT,allocated_minor INTEGER);
CREATE TABLE fx_daily(currency TEXT,business_date TEXT,cny_rate_millionths INTEGER,PRIMARY KEY(currency,business_date));
CREATE TABLE metric_rules(rule_id TEXT,version INTEGER,state TEXT,valid_from TEXT,valid_to TEXT,body TEXT);
CREATE TABLE etl_lineage(output_table TEXT,output_column TEXT,source_tables TEXT,expression TEXT,version INTEGER);
CREATE TABLE partition_manifest(source TEXT,business_date TEXT,state TEXT,as_of TEXT);
CREATE TABLE legacy_daily_report(shop_id INTEGER,business_date TEXT,grouping_level TEXT,category TEXT,gmv_cny_cents INTEGER,definition_version INTEGER);
CREATE INDEX ix_orders_scope ON orders(shop_id,payment_date_local,status,is_test);
CREATE INDEX ix_items_order ON order_items(order_id);
CREATE INDEX ix_items_product ON order_items(product_id,order_id);
CREATE INDEX ix_refund_item ON refund_events(item_id,refund_id,version);
CREATE INDEX ix_orders_customer ON orders(customer_ref,payment_date_local);
CREATE INDEX ix_tag_customer ON customer_tags(canonical_id,tag);
'''
PLATFORM='''
CREATE TABLE traffic_daily(shop_id INTEGER,business_date TEXT,channel TEXT,visitor_days INTEGER,paid_user_days INTEGER,version INTEGER,is_final INTEGER);
CREATE TABLE ad_daily(shop_id INTEGER,business_date TEXT,campaign_id TEXT,spend_cny_cents INTEGER,state TEXT);
CREATE TABLE inventory_snapshots(shop_id INTEGER,warehouse_id TEXT,product_id INTEGER,snapshot_at TEXT,on_hand INTEGER,reserved INTEGER,damaged INTEGER);
CREATE TABLE purchase_orders(po_id TEXT,shop_id INTEGER,product_id INTEGER,quantity INTEGER,status TEXT,eta TEXT);
CREATE TABLE supplier_terms(product_id INTEGER PRIMARY KEY,lead_days INTEGER,pack_size INTEGER,min_order INTEGER,unit_cost_cny_cents INTEGER);
CREATE TABLE finance_entries(event_id INTEGER PRIMARY KEY,journal_id TEXT,version INTEGER,order_id INTEGER,shop_id INTEGER,component TEXT,posted_date TEXT,amount_cny_cents INTEGER,state TEXT);
CREATE INDEX ix_finance_scope ON finance_entries(shop_id,posted_date,component);
CREATE INDEX ix_finance_version ON finance_entries(journal_id,version);
CREATE INDEX ix_ads ON ad_daily(shop_id,business_date,campaign_id);
CREATE INDEX ix_inventory ON inventory_snapshots(shop_id,product_id,snapshot_at);
'''

def build(out, n, seed):
 if out.exists():raise SystemExit('Output exists; use a NEW --out directory to preserve reproducibility.')
 (out/'public').mkdir(parents=True);(out/'private').mkdir()
 w=connect(out/'public/warehouse.sqlite');p=connect(out/'public/platform.sqlite')
 w.executescript(SCHEMA);p.executescript(PLATFORM)
 rng=random.Random(seed); days=[START+timedelta(days=i) for i in range((CUT-START).days)]
 fx={}
 for d in days+[CUT+timedelta(days=i) for i in range(46)]:
  for c,rate in [('CNY',1000000),('USD',7100000+(d.toordinal()%9)*12000),('HKD',905000+(d.toordinal()%7)*1000)]:
   fx[c,iso(d)]=rate
 w.executemany('INSERT INTO fx_daily VALUES(?,?,?)',[(c,d,r) for (c,d),r in fx.items()])
 products=20000
 for pid in range(1,products+1):
  cat=CATS[(pid-1)%5]; changed=CATS[pid%5] if pid%11==0 else cat
  w.executemany('INSERT INTO products_scd VALUES(?,?,?,?,?,?)',[(pid,1,f'SKU-{pid:05d}',cat,'2020-01-01','2026-09-01'),(pid,2,f'SKU-{pid:05d}',changed,'2026-09-01','2099-01-01')])
 identities=n//3+2
 w.executemany('INSERT INTO customer_identity VALUES(?,?)',((f'{prefix}-{uid}',uid) for uid in range(1,identities+1) for prefix in ('app','web')))
 w.executemany('INSERT INTO customer_tags VALUES(?,?,?,?)',((uid,'employee','2020-01-01','2099-01-01') for uid in range(97,identities+1,97)))
 # Oracle sums are updated from logical business events BEFORE redundant physical rows are emitted.
 weekly=defaultdict(int);catweek=defaultdict(int);campaign=defaultdict(lambda:defaultdict(int));sales=defaultdict(lambda:defaultdict(int));customer_orders=defaultdict(list)
 bridge=defaultdict(int);dailygross=defaultdict(int);finid=payid=refid=itemid=0
 buffers={k:[] for k in ['orders','order_items','payment_events','refund_events','coupon_allocations']}; fbuf=[]
 def finance(oid,shop,component,dt,amount):
  nonlocal finid
  key=f'{oid}-{component}'
  # An obsolete revision and replay of the same logical posting must not be summed.
  finid+=1;fbuf.append((finid,key,1,oid,shop,component,dt,amount+7,'posted'))
  finid+=1;fbuf.append((finid,key,2,oid,shop,component,dt,amount,'posted'))
  if shop==1 and '2026-09-01'<=dt<'2026-09-13':
   bridge['cash_net_cents']+=amount
   if component!='goods':bridge[component+'_cash_cents']+=amount
 def flush():
  for table,rows in buffers.items():
   if rows:
    w.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows);rows.clear()
  if fbuf:p.executemany('INSERT INTO finance_entries VALUES(?,?,?,?,?,?,?,?,?)',fbuf);fbuf.clear()
  w.commit();p.commit()
 for oid in range(1,n+1):
  uid=(oid-1)//3+1
  # Skew: 40% of orders belong to flagship shop 1; 99 other shops are distractors.
  shop=1 if uid%10<4 else 2+uid%99
  d=days[rng.randrange(len(days))]; ds=iso(d); channel=['search','paid','recommend','direct'][uid%4]
  curr=ds>='2026-09-06';basecat=CATS[(uid-1)%5]
  status='paid' if rng.random()>(0.35 if curr and shop==1 and basecat=='家电' else 0.08) else 'cancelled'
  test=int(oid%101==0); currency='USD' if oid%23==0 else ('HKD' if oid%29==0 else 'CNY')
  camp=f'C{uid%3+1:02d}'; hour=oid%24; minute=oid%60
  local=datetime(d.year,d.month,d.day,hour,minute); utc=local-timedelta(hours=8)
  ref=f'{"app" if oid%2 else "web"}-{uid}'; shipping=0 if oid%3 else 800
  buffers['orders'].append((oid,shop,ref,utc.isoformat(timespec='seconds'),ds,status,currency,test,camp,channel,shipping))
  gmv=cost=refund=returned_cost=qtysum=goodsminor=discounttotal=0;refund_cash=[]
  for j in range(2+oid%3):
   itemid+=1
   pid=1+(uid+j*17)%120 if oid%5<3 else 121+(uid*13+j*71)%(products-120)
   category=CATS[pid%5] if pid%11==0 and ds>='2026-09-01' else CATS[(pid-1)%5]
   qty=1+(oid+j)%3; unit=3000+(pid%80)*250
   gross=unit*qty; discount=gross*(8 if camp=='C02' else 2)//10
   paid=gross-discount
   if curr and shop==1 and category=='家电':paid=paid*6//10
   # Local-currency prices correspond to comparable RMB SKU prices.
   paid=paid if currency=='CNY' else paid*1000000//fx[currency,ds]
   gross=gross if currency=='CNY' else gross*1000000//fx[currency,ds];discount=gross-paid;discounttotal+=discount
   cny=cents(paid,fx[currency,ds]);unitcost=1200+(pid%80)*100
   buffers['order_items'].append((itemid,oid,pid,qty,gross,discount,paid,unitcost))
   gmv+=cny;goodsminor+=paid;cost+=qty*unitcost;qtysum+=qty
   refunded=refundqty=0
   if oid%89==0 or (oid+j)% (6 if camp=='C02' else 23)==0:
    rd=d+timedelta(days=3+(oid%17));rs=iso(rd)
    settled=rd<CUT and status=='paid'
    refundqty=qty if oid%4==0 or oid%89==0 else 1
    rm=paid*refundqty//qty;refunded=cents(rm,fx[currency,ds]) if settled else 0
    rid=f'R-{itemid}'
    refid+=1;buffers['refund_events'].append((refid,rid,itemid,1,'requested',refundqty,rm,rs))
    refid+=1;buffers['refund_events'].append((refid,rid,itemid,2,'settled' if settled else 'pending',refundqty,rm,rs))
    if oid%11==0:
     refid+=1;buffers['refund_events'].append((refid,rid,itemid,2,'settled' if settled else 'pending',refundqty,rm,rs))
    if settled:refund_cash.append((rs,cents(rm,fx[currency,rs])))
    refund+=refunded;returned_cost+=refundqty*unitcost if settled else 0
   if shop==1 and status=='paid' and not test:
    week='current' if '2026-09-06'<=ds<'2026-09-13' else 'previous' if '2026-08-30'<=ds<'2026-09-06' else None
    if week:weekly[week]+=cny;catweek[week,category]+=cny
    if '2026-08-16'<=ds<'2026-09-13' and pid<=10:
     sales[pid]['qty']+=qty-(refundqty if refunded else 0)
     sales[pid]['margin']+=cny-refunded-qty*unitcost+(refundqty*unitcost if refunded else 0)
  buffers['coupon_allocations'].extend([(oid,'platform_coupon',discounttotal//3),(oid,'merchant_coupon',discounttotal-discounttotal//3)])
  for ver,st,amt in [(1,'failed',goodsminor),(2,'captured' if status=='paid' else 'cancelled',goodsminor)]:
   payid+=1;buffers['payment_events'].append((payid,f'P-{oid}',oid,ver,st,amt,ds))
  if shop==1 and status=='paid' and not test:
   dailygross[ds]+=gmv
   if '2026-08-16'<=ds<'2026-09-01':
    a=campaign[camp];a['gmv_cents']+=gmv;a['refund_cents']+=refund;a['net_cogs_cents']+=cost-returned_cost
   if uid%97 and gmv-refund>0:customer_orders[uid].append((ds,oid,channel))
   if '2026-09-01'<=ds<'2026-09-13':bridge['operating_gmv_cents']+=gmv
  if status=='paid' and not test:
   # Per-order seeded settlement lag has no arithmetic relation to order_id.
   lag=1+int.from_bytes(hashlib.blake2b(f'{seed}:{oid}:settlement'.encode(),digest_size=8).digest(),'big')%5
   settle=d+timedelta(days=lag);ss=iso(settle)
   if settle<CUT:
    actual=cents(goodsminor,fx[currency,ss]);fee=(actual+shipping)*2//100
    finance(oid,shop,'goods',ss,actual);finance(oid,shop,'shipping',ss,shipping);finance(oid,shop,'fees',ss,-fee)
    if shop==1 and '2026-09-01'<=ss<'2026-09-13':
     if ds<'2026-09-01':bridge['prior_goods_cash_cents']+=actual
     else:bridge['current_goods_fx_rounding_cents']+=actual-gmv
   if shop==1 and '2026-09-01'<=ds<'2026-09-13' and settle>=CUT:bridge['current_goods_not_settled_cents']+=gmv
   # Consolidated per-order/day returns, journal identity stays unique per component.
   rr=defaultdict(int)
   for rd,a in refund_cash:rr[rd]+=a
   for rd,a in rr.items():finance(oid,shop,'refunds_'+rd,rd,-a)
  if oid%10000==0:flush()
  if oid%100000==0:print(f'generated orders={oid:,}',flush=True)
 flush()
 # Platform sources are physically separate. Raw traffic snapshots include obsolete revisions.
 visitors=defaultdict(int);ads=defaultdict(int)
 for shop in range(1,101):
  for d in days:
   ds=iso(d)
   for ch in ['search','paid','recommend','direct']:
    v=900+(d.toordinal()*17+shop*31+len(ch)*19)%700
    if shop==1 and ds>='2026-09-06' and ch=='paid':v=v*55//100
    p.executemany('INSERT INTO traffic_daily VALUES(?,?,?,?,?,?,?)',[(shop,ds,ch,v+90,v//20,1,0),(shop,ds,ch,v,v//25,2,1)])
    if shop==1 and '2026-08-30'<=ds<'2026-09-13':visitors['current' if ds>='2026-09-06' else 'previous']+=v
   for ca in ['C01','C02','C03']:
    spend=int(n/1000000*(450000 if ca=='C02' else 120000 if ca=='C01' else 80000))+(d.toordinal()%11)*100
    p.execute('INSERT INTO ad_daily VALUES(?,?,?,?,?)',(shop,ds,ca,spend,'final'))
    p.execute('INSERT INTO ad_daily VALUES(?,?,?,?,?)',(shop,ds,ca,spend+10000,'estimate'))
    if shop==1 and '2026-08-16'<=ds<'2026-09-01':ads[ca]+=spend
 inv={};incoming={};terms={}
 for pid in range(1,products+1):
  lead=3+pid%8;pack=[6,10,12][pid%3];moq=pack*2;cost=1200+(pid%80)*100
  p.execute('INSERT INTO supplier_terms VALUES(?,?,?,?,?)',(pid,lead,pack,moq,cost))
  avail=0
  for wh in ['EAST','SOUTH']:
   stock=2+(pid*17+(0 if wh=='EAST' else 5))%31;reserve=pid%3;damage=pid%2
   for stamp,extra in [('2026-09-12T08:00:00',200),('2026-09-13T08:00:00',0)]:
    p.execute('INSERT INTO inventory_snapshots VALUES(?,?,?,?,?,?,?)',(1,wh,pid,stamp,stock+extra,reserve,damage))
   avail+=stock-reserve-damage
  inc=0
  for typ,offset,q in [('confirmed',2,pack),('cancelled',1,pack*20),('confirmed',lead+20,pack*30)]:
   eta=iso(CUT+timedelta(days=offset));p.execute('INSERT INTO purchase_orders VALUES(?,?,?,?,?,?)',(f'PO-{pid}-{typ}-{offset}',1,pid,q,typ,eta))
   if typ=='confirmed' and offset<=lead+10:inc+=q
  if pid<=10:inv[pid]=avail;incoming[pid]=inc;terms[pid]=(lead,pack,moq,cost)
 p.commit()
 # Clearly versioned public business rules, not case-specific answers.
 rules=[('gmv',3,'approved','2026-01-01','2099-01-01','仅paid且非测试；订单支付日北京时间；商品行已扣优惠，不含运费，不冲减退款；汇率用支付日，按行四舍五入到分；分类按支付日匹配SCD有效期。'),('gmv',1,'retired','2020-01-01','2026-01-01','旧看板把运费计入销售额，禁止用于当前定义。'),('refund',2,'approved','2026-01-01','2099-01-01','按refund_id最高version，version相同取最高event_id；settled且settled_date<2026-09-13；经营口径退款用原支付日汇率，退货成本=退款件数×原订单行成本。'),('cohort',1,'approved','2026-01-01','2099-01-01','canonical_id合并app/web；排除employee、测试、未支付、截至观察日全额退款订单；首购为剩余订单最早时间；30日复购为首购日后且第30日前的另一订单；first_date+30<=2026-09-13才进入成熟分母。'),('inventory',1,'approved','2026-01-01','2099-01-01','仅shop=1；快照截至2026-09-13T08:00:00按仓×商品取最新；可售=on_hand-reserved-damaged；近28天净销量扣已结算退货；目标库存ceil(净销量*(lead_days+10)/28)；扣可售和覆盖期内confirmed在途；按整包装向上取整且满足min_order；按近28天净商品毛利降序分配5000000分采购预算，平局product_id升序；剩余预算不足MOQ则跳过。'),('profit',1,'approved','2026-01-01','2099-01-01','活动商品贡献=优惠后GMV-已结算退款-扣除退货后的商品成本-同窗口final广告支出；不是增量利润，不能证明广告因果效果；不包含平台费用、物流、人力。'),('finance',1,'approved','2026-01-01','2099-01-01','journal_id取最高version及event_id；仅posted且posted_date在窗口；goods/shipping为正、fees/refunds为负；按结算日汇率，经营GMV按支付日汇率。')]
 w.executemany('INSERT INTO metric_rules VALUES(?,?,?,?,?,?)',rules)
 w.executemany('INSERT INTO etl_lineage VALUES(?,?,?,?,?)', [('legacy_daily_report','gmv_cny_cents','orders,order_items','历史v1含运费；TOTAL/CATEGORY重复层级',1),('paid_gmv','amount','orders,order_items,fx_daily,products_scd','SUM(ROUND(paid_minor * cny_rate_millionths / 1000000))',3)])
 for d in days:
  for src in ['orders','order_items','refund_events','finance_entries','ad_daily','traffic_daily']:
   w.execute('INSERT INTO partition_manifest VALUES(?,?,?,?)',(src,iso(d),'complete','2026-09-13T08:00:00+08:00'))
 for ds,g in dailygross.items():
  w.executemany('INSERT INTO legacy_daily_report VALUES(?,?,?,?,?,?)',[(1,ds,'TOTAL',None,g+10000,1),(1,ds,'CATEGORY','ALL',g+10000,1)])
 w.commit();w.execute('ANALYZE');p.execute('ANALYZE');w.commit();p.commit()
 c1={'previous_gmv_cents':weekly['previous'],'current_gmv_cents':weekly['current'],'delta_gmv_cents':weekly['current']-weekly['previous'],'previous_visitor_days':visitors['previous'],'current_visitor_days':visitors['current'],'category_delta_cents':{c:catweek['current',c]-catweek['previous',c] for c in CATS}}
 c1['worst_category']=min(CATS,key=lambda c:c1['category_delta_cents'][c])
 c2={}
 for ca in ['C01','C02','C03']:
  a=dict(campaign[ca]);a['ad_spend_cents']=ads[ca];a['contribution_cents']=a.get('gmv_cents',0)-a.get('refund_cents',0)-a.get('net_cogs_cents',0)-ads[ca];c2[ca]=a
 c2={'campaigns':c2,'best_campaign':max(c2,key=lambda x:c2[x]['contribution_cents'])}
 remaining=5000000;plan={}
 for pid in sorted(range(1,11),key=lambda x:(-sales[x]['margin'],x)):
  lead,pack,moq,cost=terms[pid];target=(max(0,sales[pid]['qty'])*(lead+10)+27)//28
  raw=max(0,target-inv[pid]-incoming[pid]);want=max(moq,((raw+pack-1)//pack)*pack) if raw else 0
  buy=min(want,(remaining//(pack*cost))*pack);buy=buy if buy>=moq else 0;remaining-=buy*cost
  plan[str(pid)]={'net_units_28d':sales[pid]['qty'],'available':inv[pid],'qualified_inbound':incoming[pid],'buy_units':buy,'purchase_cents':buy*cost}
 c3={'products':plan,'total_purchase_cents':5000000-remaining}
 coh={'2026-07':{'eligible_customers':0,'repeat_customers':0,'immature_customers':0},'2026-08':{'eligible_customers':0,'repeat_customers':0,'immature_customers':0}}
 for uid,orders in customer_orders.items():
  orders.sort();first=date.fromisoformat(orders[0][0]);month=iso(first)[:7]
  if month not in coh:continue
  a=coh[month]
  if first+timedelta(days=30)>CUT:a['immature_customers']+=1;continue
  a['eligible_customers']+=1
  a['repeat_customers']+=int(any(first<date.fromisoformat(d)<first+timedelta(days=30) for d,_,_ in orders[1:]))
 for a in coh.values():a['repeat_rate']=ratio(a['repeat_customers'],a['eligible_customers'])
 c4={'cohorts':coh}
 # Collapse separate day-suffixed return journal components into a single signed bridge row.
 bridge['refunds_cash_cents']=sum(v for k,v in bridge.items() if k.startswith('refunds_') and k!='refunds_cash_cents')
 bridge={k:v for k,v in bridge.items() if not (k.startswith('refunds_2026'))}
 for k in ['operating_gmv_cents','cash_net_cents','shipping_cash_cents','fees_cash_cents','refunds_cash_cents','prior_goods_cash_cents','current_goods_fx_rounding_cents','current_goods_not_settled_cents']:bridge.setdefault(k,0)
 assert bridge['cash_net_cents']==bridge['operating_gmv_cents']-bridge['current_goods_not_settled_cents']+bridge['prior_goods_cash_cents']+bridge['current_goods_fx_rounding_cents']+bridge['shipping_cash_cents']+bridge['fees_cash_cents']+bridge['refunds_cash_cents']
 expected={'C1':c1,'C2':c2,'C3':c3,'C4':c4,'C5':bridge}
 dump(out/'private/expected.json',expected)
 catalog={}
 for name,conn in [('warehouse',w),('platform',p)]:
  catalog[name]={t: {'columns':[r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')],'rows':conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]} for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
 dump(out/'public/catalog.json',catalog)
 for name,conn in [('warehouse',w),('platform',p)]:
  assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok';conn.close()
 manifest={'seed':seed,'orders':n,'period':[iso(START),iso(CUT)],'as_of':'2026-09-13T08:00:00+08:00','tables':catalog,'scale_description':'构造业务数据，非真实商户生产数据；数据量不等于生产性能认证','bytes':sum(f.stat().st_size for f in (out/'public').glob('*.sqlite'))}
 manifest['database_sha256']={f.name:hashlib.file_digest(f.open('rb'),'sha256').hexdigest() for f in (out/'public').glob('*.sqlite')}
 dump(out/'manifest.json',manifest)
 print(json.dumps({'out':str(out),'orders':n,'rows':sum(t['rows'] for db in catalog.values() for t in db.values()),'bytes':manifest['bytes']},ensure_ascii=False),flush=True)

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--out',type=Path,required=True);a.add_argument('--orders',type=int,default=1000000);a.add_argument('--seed',type=int,default=9142026);args=a.parse_args();build(args.out.resolve(),args.orders,args.seed)
