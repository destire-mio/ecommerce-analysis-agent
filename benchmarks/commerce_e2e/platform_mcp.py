"""Benchmark data adapter: generic readonly SQL over a separate raw platform store."""
import json, sqlite3, time
import sys
from pathlib import Path
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p/'sql_control.py').exists())))
import sql_control
import sqlglot
from sqlglot import exp
from mcp.server.fastmcp import FastMCP

DB=Path('data/platform/platform_data.sqlite')
mcp=FastMCP('commerce-benchmark-platform')

def output(x):return json.dumps(x,ensure_ascii=False)

def query(sql, limit=200, timeout_seconds=5):
    parsed=sqlglot.parse(sql,read='sqlite')
    if len(parsed)!=1 or not isinstance(parsed[0],exp.Query):
        return {'status':'error','error':'one read-only SELECT required'}
    result=sql_control.run_bounded(sql, DB, seconds=timeout_seconds, limit=limit)
    if result.get('error'):
        return result
    result['rows']=result.pop('rows_all')
    result.update(source='platform',snapshot='2026-09-13T08:00:00+08:00')
    return result

@mcp.tool()
def list_platform_tables() -> str:
    """列出平台原始表、字段。平台库与订单库分离；字段中的 cents 是人民币分。"""
    c=sqlite3.connect(f'file:{DB.resolve()}?mode=ro',uri=True)
    try:
        data={r[0]:[list(x) for x in c.execute('PRAGMA table_info("'+r[0]+'")')] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        return output({'status':'ok','tables':data})
    finally:c.close()

@mcp.tool()
def query_platform(sql: str, timeout_seconds: float = 5) -> str:
    """执行平台库只读 SQLite SQL。最多返回200行；超过上限明确报错，可按业务键分页。可查询traffic_daily/ad_daily/inventory_snapshots/purchase_orders/supplier_terms/finance_entries。只提供原始数据，不提供用例答案；数据口径见 commerce_contract 技能。"""
    try:return output(query(sql, timeout_seconds=timeout_seconds))
    except Exception as e:return output({'status':'error','error':type(e).__name__,'detail':str(e)})

if __name__=='__main__':mcp.run(transport='stdio')
