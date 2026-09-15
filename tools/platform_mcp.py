"""mock 平台数据 MCP server(对标 mcp-cn-commerce 的形态, stdio 协议).

数据: data/platform/platform_data.sqlite (mock 电商后台, 与本地库物理隔离).
工具(生意参谋口径):
    get_market_overview(industry, start, end)  行业大盘: 平台访客/GMV/转化率
    get_shop_traffic(start, end, source?)      自店渠道流量拆解
    get_funnel(start, end)                     转化漏斗(曝光→点击→详情→加购→支付)
未提供的能力(如竞品/库存): 返回 not_available, agent 如实转告用户.
"""
import json
import os
import sqlite3

from mcp.server.fastmcp import FastMCP

DB = os.path.join("data", "platform", "platform_data.sqlite")

mcp = FastMCP("platform-data")


def q(sql: str, params=()):
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(sql, params).fetchall()
        return rows
    finally:
        conn.close()


@mcp.tool()
def get_market_overview(start: str, end: str, industry: str = "家电/其他行业合计") -> str:
    """行业大盘: 指定日期区间内平台访客/平台GMV/行业转化率, 按天返回."""
    rows = q("""SELECT date, platform_visitors, platform_gmv, industry_conversion
                FROM market_overview
                WHERE industry=? AND date>=? AND date<? ORDER BY date""",
             (industry, start, end))
    if not rows:
        return json.dumps({"error": "no_data", "note": "该区间无大盘数据"}, ensure_ascii=False)
    total_v = sum(r[1] for r in rows)
    total_g = sum(r[2] for r in rows)
    avg_cvr = sum(r[3] for r in rows) / len(rows)
    return json.dumps({
        "industry": industry,
        "daily": [{"date": r[0], "visitors": r[1], "gmv": r[2], "cvr": round(r[3], 4)} for r in rows],
        "summary": {"visitors": total_v, "gmv": total_g, "avg_conversion": round(avg_cvr, 4)},
    }, ensure_ascii=False)


@mcp.tool()
def get_shop_traffic(start: str, end: str, source: str = None) -> str:
    """自店渠道流量(生意参谋口径): 手淘搜索/直通车/手淘推荐/其他渠道, 按天×渠道."""
    if source:
        rows = q("""SELECT date, source, visitors FROM shop_traffic
                    WHERE date>=? AND date<? AND source=? ORDER BY date, source""",
                 (start, end, source))
    else:
        rows = q("""SELECT date, source, visitors FROM shop_traffic
                    WHERE date>=? AND date<? ORDER BY date, source""", (start, end))
    if not rows:
        return json.dumps({"error": "no_data", "note": "该区间无流量数据"}, ensure_ascii=False)
    by_source = {}
    for d, s, v in rows:
        by_source.setdefault(s, 0)
        by_source[s] += v
    total = sum(by_source.values())
    return json.dumps({
        "daily": [{"date": r[0], "source": r[1], "visitors": r[2]} for r in rows],
        "by_source": by_source,
        "total_visitors": total,
    }, ensure_ascii=False)


@mcp.tool()
def get_funnel(start: str, end: str) -> str:
    """转化漏斗: 曝光→点击→详情页→加购→支付 的两期各节点量."""
    rows = q("""SELECT stage, SUM(count) FROM funnel
                WHERE date>=? AND date<? GROUP BY stage""", (start, end))
    order = ["曝光", "点击", "详情页", "加购", "支付"]
    stages = {s: c for s, c in rows}
    if not stages:
        return json.dumps({"error": "no_data"}, ensure_ascii=False)
    result = [{"stage": s, "count": stages.get(s, 0)} for s in order if s in stages]
    return json.dumps({"funnel": result}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
