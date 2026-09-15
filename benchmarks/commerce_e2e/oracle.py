"""Independent SQL reconstruction vs generator's streaming logical-event oracle."""
from pathlib import Path
import sqlite3,json,argparse,time,math
from collections import defaultdict

def reconstruct(root):
 c=sqlite3.connect(f'file:{root}/public/warehouse.sqlite?mode=ro',uri=True)
 c.execute('ATTACH DATABASE ? AS platform',(f'file:{root}/public/platform.sqlite?mode=ro',))
 c.executescript('''
 CREATE TEMP VIEW latest_refund AS SELECT * FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY refund_id ORDER BY version DESC,event_id DESC) rn FROM refund_events) WHERE rn=1 AND state='settled' AND settled_date<'2026-09-13';
 CREATE TEMP VIEW item_refund AS SELECT item_id,SUM(refund_minor) refunded_minor,SUM(refund_quantity) refunded_qty FROM latest_refund GROUP BY item_id;
 CREATE TEMP VIEW paid AS SELECT o.order_id,o.customer_ref,o.payment_date_local d,o.campaign_id,o.channel,i.item_id,i.product_id,i.quantity,i.unit_cost_cny_cents,
 CAST((i.paid_minor*f.cny_rate_millionths+500000)/1000000 AS INTEGER) amount,
 CAST((COALESCE(r.refunded_minor,0)*f.cny_rate_millionths+500000)/1000000 AS INTEGER) refund,
 COALESCE(r.refunded_qty,0) returned_qty,s.category
 FROM orders o JOIN order_items i ON i.order_id=o.order_id JOIN fx_daily f ON f.business_date=o.payment_date_local AND f.currency=o.currency
 JOIN products_scd s ON s.product_id=i.product_id AND s.valid_from<=o.payment_date_local AND s.valid_to>o.payment_date_local
 LEFT JOIN item_refund r ON r.item_id=i.item_id
 WHERE o.shop_id=1 AND o.status='paid' AND o.is_test=0;
 CREATE TEMP VIEW latest_finance AS SELECT * FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY journal_id ORDER BY version DESC,event_id DESC) rn FROM platform.finance_entries) WHERE rn=1 AND state='posted' AND shop_id=1;
 ''')
 def scalar(sql,args=()):return c.execute(sql,args).fetchone()[0] or 0
 c1={}
 for period,start,end in [('previous','2026-08-30','2026-09-06'),('current','2026-09-06','2026-09-13')]:
  c1[period+'_gmv_cents']=scalar('SELECT SUM(amount) FROM paid WHERE d>=? AND d<?',(start,end))
  c1[period+'_visitor_days']=scalar('SELECT SUM(visitor_days) FROM platform.traffic_daily WHERE shop_id=1 AND is_final=1 AND business_date>=? AND business_date<?',(start,end))
 cats=['家电','服饰','家居','美妆','食品'];delta={x:0 for x in cats}
 for cat,val in c.execute("SELECT category,SUM(CASE WHEN d>='2026-09-06' THEN amount ELSE -amount END) FROM paid WHERE d>='2026-08-30' GROUP BY category"):delta[cat]=val
 c1['delta_gmv_cents']=c1['current_gmv_cents']-c1['previous_gmv_cents'];c1['category_delta_cents']=delta;c1['worst_category']=min(cats,key=lambda x:delta[x])
 cs={}
 for ca in ['C01','C02','C03']:
  vals=c.execute("SELECT SUM(amount),SUM(refund),SUM((quantity-returned_qty)*unit_cost_cny_cents) FROM paid WHERE campaign_id=? AND d>='2026-08-16' AND d<'2026-09-01'",(ca,)).fetchone()
  a=dict(zip(['gmv_cents','refund_cents','net_cogs_cents'],[x or 0 for x in vals]));a['ad_spend_cents']=scalar("SELECT SUM(spend_cny_cents) FROM platform.ad_daily WHERE shop_id=1 AND campaign_id=? AND state='final' AND business_date>='2026-08-16' AND business_date<'2026-09-01'",(ca,));a['contribution_cents']=a['gmv_cents']-a['refund_cents']-a['net_cogs_cents']-a['ad_spend_cents'];cs[ca]=a
 c2={'campaigns':cs,'best_campaign':max(cs,key=lambda x:cs[x]['contribution_cents'])}
 sales={pid:(q,m) for pid,q,m in c.execute("SELECT product_id,SUM(quantity-returned_qty),SUM(amount-refund-(quantity-returned_qty)*unit_cost_cny_cents) FROM paid WHERE product_id<=10 AND d>='2026-08-16' GROUP BY product_id")}
 terms={row[0]:row[1:] for row in c.execute('SELECT product_id,lead_days,pack_size,min_order,unit_cost_cny_cents FROM platform.supplier_terms WHERE product_id<=10')}
 stock=dict(c.execute("SELECT product_id,SUM(on_hand-reserved-damaged) FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY shop_id,warehouse_id,product_id ORDER BY snapshot_at DESC) rn FROM platform.inventory_snapshots WHERE shop_id=1 AND snapshot_at<='2026-09-13T08:00:00') WHERE rn=1 GROUP BY product_id"))
 remain=5000000;plan={}
 for pid in sorted(range(1,11),key=lambda x:(-sales.get(x,(0,0))[1],x)):
  lead,pack,moq,cost=terms[pid];q,m=sales.get(pid,(0,0));inbound=scalar("SELECT SUM(quantity) FROM platform.purchase_orders WHERE shop_id=1 AND product_id=? AND status='confirmed' AND eta<=date('2026-09-13',?)",(pid,f'+{lead+10} days'))
  deficit=max(0,math.ceil(max(0,q)*(lead+10)/28)-stock[pid]-inbound)
  target=max(moq,math.ceil(deficit/pack)*pack) if deficit else 0
  buy=min(target,(remain//cost//pack)*pack)
  if buy<moq:buy=0
  remain-=buy*cost
  plan[str(pid)]={'net_units_28d':q,'available':stock[pid],'qualified_inbound':inbound,'buy_units':buy,'purchase_cents':buy*cost}
 c3={'products':plan,'total_purchase_cents':5000000-remain}
 c.executescript('''
 CREATE TEMP VIEW valid_orders AS SELECT p.order_id,p.d,ci.canonical_id FROM paid p JOIN customer_identity ci ON ci.customer_ref=p.customer_ref
 WHERE NOT EXISTS(SELECT 1 FROM customer_tags t WHERE t.canonical_id=ci.canonical_id AND t.tag='employee' AND t.valid_from<=p.d AND t.valid_to>p.d)
 GROUP BY p.order_id,p.d,ci.canonical_id HAVING SUM(p.amount-p.refund)>0;
 CREATE TEMP VIEW first_orders AS SELECT canonical_id,MIN(d) first_date FROM valid_orders GROUP BY canonical_id;
 ''')
 # Materialize benchmark-private intermediate once to avoid O(N^2) correlated view expansion.
 c.execute('CREATE TEMP TABLE valid_rows AS SELECT * FROM valid_orders');c.execute('CREATE INDEX idx_valid_canonical ON valid_rows(canonical_id,d)')
 coh={}
 for mon in ['2026-07','2026-08']:
  total,rep,imm=c.execute("""SELECT SUM(date(first_date,'+30 days')<='2026-09-13'),SUM(CASE WHEN date(first_date,'+30 days')<='2026-09-13' AND EXISTS(SELECT 1 FROM valid_rows v WHERE v.canonical_id=f.canonical_id AND v.d>f.first_date AND v.d<date(f.first_date,'+30 days')) THEN 1 ELSE 0 END),SUM(date(first_date,'+30 days')>'2026-09-13') FROM (SELECT canonical_id,MIN(d) first_date FROM valid_rows GROUP BY canonical_id) f WHERE substr(first_date,1,7)=?""",(mon,)).fetchone()
  total,rep,imm=total or 0,rep or 0,imm or 0
  coh[mon]={'eligible_customers':total,'repeat_customers':rep,'immature_customers':imm,'repeat_rate':round(rep/total,8) if total else None}
 c4={'cohorts':coh}
 c.execute('CREATE TEMP TABLE fj AS SELECT * FROM latest_finance');c.execute('CREATE INDEX idx_fj_order ON fj(order_id,component,posted_date)')
 c.execute('CREATE TEMP TABLE pg AS SELECT order_id,d,SUM(amount) gmv FROM paid GROUP BY order_id,d');c.execute('CREATE INDEX idx_pg_order ON pg(order_id,d)')
 c5={'operating_gmv_cents':scalar("SELECT SUM(gmv) FROM pg WHERE d>='2026-09-01'")}
 window="posted_date>='2026-09-01' AND posted_date<'2026-09-13'"
 c5['cash_net_cents']=scalar('SELECT SUM(amount_cny_cents) FROM fj WHERE '+window)
 for comp in ['shipping','fees','refunds']:
  c5[comp+'_cash_cents']=scalar('SELECT SUM(amount_cny_cents) FROM fj WHERE '+window+' AND component LIKE ?',(comp+'%',))
 c5['prior_goods_cash_cents']=scalar("SELECT SUM(j.amount_cny_cents) FROM fj j JOIN orders o ON o.order_id=j.order_id WHERE j.component='goods' AND j.posted_date>='2026-09-01' AND o.payment_date_local<'2026-09-01'")
 c5['current_goods_fx_rounding_cents']=scalar("SELECT SUM(j.amount_cny_cents-g.gmv) FROM fj j JOIN pg g ON g.order_id=j.order_id WHERE j.component='goods' AND j.posted_date>='2026-09-01' AND g.d>='2026-09-01'")
 c5['current_goods_not_settled_cents']=scalar("SELECT SUM(gmv) FROM pg g WHERE d>='2026-09-01' AND NOT EXISTS(SELECT 1 FROM fj j WHERE j.order_id=g.order_id AND j.component='goods')")
 # Explain the scoped query; aggregates over the whole period can legitimately scan matching rows.
 explain=[list(r) for r in c.execute("EXPLAIN QUERY PLAN SELECT SUM(i.paid_minor) FROM orders o JOIN order_items i ON i.order_id=o.order_id WHERE o.shop_id=1 AND o.status='paid' AND o.is_test=0 AND o.payment_date_local>='2026-09-06'")]
 c.close();return {'C1':c1,'C2':c2,'C3':c3,'C4':c4,'C5':c5},explain

def compare(expected,actual,path=''):
 errors=[]
 if isinstance(expected,dict):
  if not isinstance(actual,dict):return [(path,'object required',actual)]
  for key in expected:
   if key not in actual:errors.append((path+'/'+key,'missing',None))
   else:errors.extend(compare(expected[key],actual[key],path+'/'+key))
 elif isinstance(expected,float):
  if type(actual) not in (int,float) or not math.isfinite(actual) or abs(expected-actual)>1e-7:errors.append((path,expected,actual))
 elif type(expected) is int and type(actual) is not int:errors.append((path,expected,actual))
 elif expected!=actual:errors.append((path,expected,actual))
 return errors
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('dataset',type=Path);args=a.parse_args();t=time.monotonic();result,plan=reconstruct(args.dataset.resolve());expected=json.loads((args.dataset/'private/expected.json').read_text());errors=compare(expected,result)
 report={'status':'PASS' if not errors else 'FAIL','method':'SQL重建对照生成期Python业务事件累加；不是Agent验收','elapsed_seconds':round(time.monotonic()-t,3),'errors':errors,'query_plan':plan}
 (args.dataset/'private/oracle_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit(bool(errors))
