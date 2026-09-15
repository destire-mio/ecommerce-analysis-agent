"""Benchmark data adapter: generic readonly SQL over a separate raw platform store."""
import json, sqlite3, time
from pathlib import Path
import sqlglot
from sqlglot import exp
from mcp.server.fastmcp import FastMCP

DB=Path('data/platform/platform_data.sqlite')
mcp=FastMCP('commerce-benchmark-platform')

def output(x):return json.dumps(x,ensure_ascii=False)

def query(sql, limit=200):
    parsed=sqlglot.parse(sql,read='sqlite')
    if len(parsed)!=1 or not isinstance(parsed[0],exp.Query):
        return {'status':'error','error':'one read-only SELECT required'}
    c=sqlite3.connect(f'file:{DB.resolve()}?mode=ro',uri=True)
    allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
    c.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    deadline=time.monotonic()+45
    c.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
    try:
        cur=c.execute(sql);rows=cur.fetchmany(limit+1)
        if len(rows)>limit:
            return {'status':'error','error':'result_too_large','limit':limit,'note':'请聚合，或用稳定业务键分页；不会把截断结果当完整结果。'}
        return {'status':'ok','source':'platform','snapshot':'2026-09-13T08:00:00+08:00',
                'columns':[x[0] for x in cur.description], 'rows':[list(r) for r in rows], 'n_rows':len(rows)}
    finally:c.close()

@mcp.tool()
def list_platform_tables() -> str:
    """列出平台原始表、字段。平台库与订单库分离；字段中的 cents 是人民币分。"""
    c=sqlite3.connect(f'file:{DB.resolve()}?mode=ro',uri=True)
    try:
        data={r[0]:[list(x) for x in c.execute('PRAGMA table_info("'+r[0]+'")')] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        return output({'status':'ok','tables':data})
    finally:c.close()

@mcp.tool()
def query_platform(sql: str) -> str:
    """执行平台库只读 SQLite SQL。最多返回200行；超过上限明确报错，可按业务键分页。可查询traffic_daily/ad_daily/inventory_snapshots/purchase_orders/supplier_terms/finance_entries。只提供原始数据，不提供用例答案；数据口径见 commerce_contract 技能。"""
    try:return output(query(sql))
    except Exception as e:return output({'status':'error','error':type(e).__name__,'detail':str(e)})

if __name__=='__main__':mcp.run(transport='stdio')
