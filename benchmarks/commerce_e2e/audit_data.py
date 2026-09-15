"""Measure whether declared business traps exist; no Agent inference involved."""
from pathlib import Path
import sqlite3,json,sys
root=Path(sys.argv[1]).resolve();c=sqlite3.connect(f'file:{root}/public/warehouse.sqlite?mode=ro',uri=True)
def count(sql):return c.execute(sql).fetchone()[0]
checks={
 'other_shop_orders':count('SELECT COUNT(*) FROM orders WHERE shop_id<>1'),
 'non_cny_orders':count("SELECT COUNT(*) FROM orders WHERE currency<>'CNY'"),
 'utc_local_different_date_orders':count('SELECT COUNT(*) FROM orders WHERE substr(pay_time_utc,1,10)<>payment_date_local'),
 'cancelled_orders':count("SELECT COUNT(*) FROM orders WHERE status='cancelled'"),
 'test_orders':count('SELECT COUNT(*) FROM orders WHERE is_test=1'),
 'changed_category_products':count('SELECT COUNT(*) FROM products_scd a JOIN products_scd b ON a.product_id=b.product_id WHERE a.version=1 AND b.version=2 AND a.category<>b.category'),
 'replayed_refund_versions':count('SELECT COUNT(*) FROM (SELECT refund_id,version FROM refund_events GROUP BY refund_id,version HAVING COUNT(*)>1)'),
 'line_amount_invariant_errors':count('SELECT COUNT(*) FROM order_items WHERE gross_minor<>paid_minor+discount_minor'),
 'coupon_allocation_invariant_errors':count('SELECT COUNT(*) FROM (SELECT order_id,SUM(discount_minor) d FROM order_items GROUP BY order_id) i JOIN (SELECT order_id,SUM(allocated_minor) a FROM coupon_allocations GROUP BY order_id) cp USING(order_id) WHERE i.d<>cp.a'),
}
c.executescript("""CREATE TEMP TABLE refunds AS SELECT item_id,SUM(refund_minor) amount FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY refund_id ORDER BY version DESC,event_id DESC) rn FROM refund_events) WHERE rn=1 AND state='settled' AND settled_date<'2026-09-13' GROUP BY item_id; CREATE INDEX idx_ref ON refunds(item_id);""")
checks['fully_refunded_paid_shop1_orders']=count("SELECT COUNT(*) FROM (SELECT o.order_id FROM orders o JOIN order_items i USING(order_id) LEFT JOIN refunds r USING(item_id) WHERE o.shop_id=1 AND o.status='paid' AND o.is_test=0 GROUP BY o.order_id HAVING SUM(i.paid_minor-COALESCE(r.amount,0))=0)")
c.execute('ATTACH DATABASE ? AS platform',(f'file:{root}/public/platform.sqlite?mode=ro',))
checks['settlement_modulo_mismatch_orders']=count("SELECT COUNT(*) FROM platform.finance_entries f JOIN orders o USING(order_id) WHERE f.component='goods' AND f.version=2 AND f.posted_date<>date(o.payment_date_local,'+'||(o.order_id%5+1)||' days')")
c.close()
status='PASS' if all(v==0 if k.endswith('_errors') else v>0 for k,v in checks.items()) else 'FAIL'
report={'status':status,'observed':checks,'scope':'Data feature and arithmetic invariant audit, not Agent completion'}
(root/'private/data_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(status!='PASS')
