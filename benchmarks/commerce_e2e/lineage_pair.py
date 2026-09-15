"""Paired dashboard provenance experiment; unchanged product, independently verified data."""
from pathlib import Path
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
import sqlite3

from build import SCHEMA
from run import execute, PRODUCT, sha, dump

TABLES = ["orders", "order_items", "fx_daily", "metric_rules", "etl_lineage",
          "partition_manifest", "legacy_daily_report", "report_adjustments"]
CONTRACT = """---
name: commerce_contract
summary: 店铺销售核对的业务口径、数据范围和报表元数据说明
---

分析 shop_id=1。观察日为2026-09-13，数据覆盖2026-08-30至2026-09-12。
日期使用北京时间 payment_date_local，区间左闭右开。分区完整性见 partition_manifest。
metric_rules 保存业务指标定义，以 approved 且在生效区间内的版本为准。
orders 每单一行，order_items 每商品行一行；paid_minor 是优惠后原币最小单位金额，
shipping_cny_cents 是订单级人民币运费分。当前支付GMV只含有效支付商品金额，不含运费。
商品金额用支付日 fx_daily 汇率，逐行四舍五入到人民币分，再求和。
报表 definition_version 是该报表的加工版本，与业务指标的版本号不自动对应。
etl_lineage 保存输出表、字段、加工版本及实际部署的SELECT查询。
report_adjustments 保存按店铺、日期登记的历史对账补差，单位为人民币分。
"""
CASE = {
    "id": "L1",
    "name": "旧看板与支付GMV差异核对",
    "question": "我是店铺1的经营负责人。比较2026-09-06至09-12与08-30至09-05，"
                "核对旧看板和当前支付GMV。给出两期重算支付GMV、旧看板金额、二者差额，"
                "解释差额来自哪项计算，并判断差异是否影响本周销售下降的结论。"
                "说明哪些原因有证据、哪些需要补证，以及下一步应该核对什么。",
    "schema": {k: "整数" for k in ["previous_gmv_cents", "current_gmv_cents",
               "previous_legacy_cents", "current_legacy_cents", "previous_gap_cents",
               "current_gap_cents", "gmv_delta_cents", "legacy_delta_cents"]},
    # No mandatory table-name gate. Causal support is reviewed from actual observations.
    "tables": [],
    "review": ["差异原因是否由实际加工证据支持，而非仅凭金额相等。",
               "缺证据时是否保留不确定性；是否区分金额水平、变化额与变化率。"],
}
BASE = """SELECT o.order_id, o.shop_id, o.payment_date_local AS business_date,
       o.shipping_cny_cents,
       SUM(CAST((i.paid_minor*f.cny_rate_millionths+500000)/1000000 AS INTEGER)) AS goods_cents
FROM orders o JOIN order_items i ON i.order_id=o.order_id
JOIN fx_daily f ON f.currency=o.currency AND f.business_date=o.payment_date_local
WHERE o.status='paid' AND o.is_test=0
GROUP BY o.order_id, o.shop_id, o.payment_date_local, o.shipping_cny_cents"""
DAILY = f"SELECT shop_id,business_date,SUM(goods_cents) AS goods_cents FROM ({BASE}) GROUP BY shop_id,business_date"
JOBS = {
    "A": f"SELECT shop_id,business_date,SUM(goods_cents+shipping_cny_cents) AS gmv_cny_cents FROM ({BASE}) WHERE shop_id=1 GROUP BY shop_id,business_date",
    "B": f"SELECT g.shop_id,g.business_date,g.goods_cents+COALESCE(a.amount_cny_cents,0) AS gmv_cny_cents FROM ({DAILY}) g LEFT JOIN report_adjustments a ON a.shop_id=g.shop_id AND a.business_date=g.business_date WHERE g.shop_id=1",
}


def read_rows(db, table):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


def build_variant(directory, variant):
    (directory / "public").mkdir(parents=True)
    db = directory / "public/warehouse.sqlite"
    conn = sqlite3.connect(db)
    for statement in SCHEMA.split(";"):
        statement = statement.strip()
        if any(statement.startswith(f"CREATE TABLE {table}(") for table in TABLES):
            conn.execute(statement)
    conn.execute("CREATE TABLE report_adjustments(shop_id INTEGER,business_date TEXT,amount_cny_cents INTEGER,reason TEXT,PRIMARY KEY(shop_id,business_date))")
    conn.execute("INSERT INTO metric_rules VALUES(?,?,?,?,?,?)", (
        "gmv", 3, "approved", "2026-01-01", "2099-01-01",
        "仅paid且非测试；支付日北京时间；商品行已扣优惠，不含运费、不减退款；按支付日汇率逐行四舍五入到人民币分。"))
    valid = []
    oid = iid = 0
    for n in range(14):
        day = (date(2026, 8, 30) + timedelta(days=n)).isoformat()
        conn.execute("INSERT INTO fx_daily VALUES('CNY',?,1000000)", (day,))
        # Both rival explanations exist in BOTH databases, with identical daily totals.
        conn.execute("INSERT INTO report_adjustments VALUES(1,?,10000,'历史订单对账补差')", (day,))
        for j in range(13):
            oid += 1
            shop = 2 if j == 12 else 1
            status = "cancelled" if j == 10 else "paid"
            test = int(j == 11)
            shipping = 1000 if j < 10 else 9999
            conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                oid, shop, f"web-{oid}", day+"T04:00:00+00:00", day, status,
                "CNY", test, None, "direct", shipping))
            amounts = (60000, 40000) if n < 7 else (48000, 32000)
            for product_id, amount in enumerate(amounts, 1):
                iid += 1
                conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?,?,?)", (
                    iid, oid, product_id, 1, amount+500, 500, amount, amount//2))
            if j < 10:
                valid.append((day, sum(amounts), shipping))
        for table in TABLES:
            if table not in ("etl_lineage", "metric_rules", "partition_manifest"):
                conn.execute("INSERT INTO partition_manifest VALUES(?,?,?,?)", (
                    table, day, "complete", "2026-09-13T08:00:00+08:00"))
    source = "orders,order_items,fx_daily" + (",report_adjustments" if variant == "B" else "")
    conn.execute("INSERT INTO etl_lineage VALUES(?,?,?,?,?)", (
        "legacy_daily_report", "gmv_cny_cents", source, JOBS[variant], 1))
    conn.execute("INSERT INTO etl_lineage VALUES(?,?,?,?,?)", (
        "paid_gmv", "amount", "orders,order_items,fx_daily", DAILY, 3))
    # Materialize the actual declared job, rather than labeling a precomputed total.
    actual_daily = conn.execute(JOBS[variant]).fetchall()
    conn.executemany("INSERT INTO legacy_daily_report VALUES(?,?,'TOTAL',NULL,?,1)", actual_daily)
    conn.commit()
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # Reference comes from logical generated events, independent of job SELECTs.
    expected_daily = {day: sum(g for d, g, _ in valid if d == day) + 10000
                      for day, _, _ in valid}
    assert {d: amount for shop, d, amount in actual_daily if shop == 1} == expected_daily
    assert len([r for r in actual_daily if r[0] == 1]) == 14
    prev = sum(g for d, g, _ in valid if d < "2026-09-06")
    curr = sum(g for d, g, _ in valid if d >= "2026-09-06")
    assert sum(s for d, _, s in valid if d < "2026-09-06") == 70000
    assert sum(s for d, _, s in valid if d >= "2026-09-06") == 70000
    conn.close()
    return {"previous_gmv_cents": prev, "current_gmv_cents": curr,
            "previous_legacy_cents": prev+70000, "current_legacy_cents": curr+70000,
            "previous_gap_cents": 70000, "current_gap_cents": 70000,
            "gmv_delta_cents": curr-prev, "legacy_delta_cents": curr-prev}


def verify_pair(root):
    paths = {v: root/v/"public/warehouse.sqlite" for v in JOBS}
    identical = []
    row_counts = {}
    for table in TABLES:
        left, right = [read_rows(paths[v], table) for v in JOBS]
        row_counts[table] = len(left)
        if table == "etl_lineage":
            assert left[1:] == right[1:]
            assert [i for i in range(5) if left[0][i] != right[0][i]] == [2, 3]
        else:
            assert left == right, table
            identical.append(table)
    with sqlite3.connect(paths["A"]) as a, sqlite3.connect(paths["B"]) as b:
        assert a.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == b.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    return {"status": "PASS", "identical_tables": identical, "row_counts": row_counts,
            "only_changed_cells": "etl_lineage legacy_daily_report row: source_tables and expression",
            "independent_oracle": "logical generated order events checked against executed report jobs",
            "weekly_shipping_cents": 70000, "weekly_adjustment_cents": 70000}


def export_trace(work):
    events = []
    for path in sorted((work/"runs").glob("*/trace.jsonl")):
        events.extend(json.loads(line) for line in path.read_text().splitlines())
    lines = ["# 工具调用 trace", "", "以下为实际工具参数与返回内容摘录；完整返回见原始 trace.jsonl。", ""]
    for event in events:
        action = event["action"]
        if action == "tool_call":
            lines += [f"## 第 {event['turn']} 轮：{event['name']}", "", "```json",
                      json.dumps(event["args"], ensure_ascii=False, indent=2), "```", ""]
        elif action == "tool_result":
            payload = json.dumps(event.get("summary", {}), ensure_ascii=False, indent=2)
            lines += ["返回：", "```json", payload[:9000] + ("\n[摘录截断]" if len(payload)>9000 else ""), "```", ""]
        elif action == "answer_validation":
            lines += ["回答校验：" + json.dumps(event, ensure_ascii=False), ""]
    (work/"trace-readable.md").write_text("\n".join(lines), encoding="utf-8")
    return {"model_calls": sum(e["action"] == "llm" for e in events),
            "tool_calls": sum(e["action"] == "tool_call" for e in events),
            "usage": {key: sum(e.get("usage", {}).get(key, 0) for e in events) for key in ("p", "c")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="call the real model for both variants")
    args = parser.parse_args()
    root = args.out.resolve()
    if root.exists():
        raise SystemExit("Use a new output directory; existing evidence is immutable.")
    if args.run and not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required; no mock fallback.")
    root.mkdir(parents=True)
    contract = root/"commerce_contract.md"
    contract.write_text(CONTRACT, encoding="utf-8")
    expected = {v: build_variant(root/v, v) for v in JOBS}
    assert expected["A"] == expected["B"]
    verification = verify_pair(root)
    dump(root/"fixture_verification.json", verification)
    dump(root/"reference.json", {"numeric": expected, "causes": {"A": "订单运费", "B": "历史对账补差"},
                               "jobs": JOBS, "scope": "Evaluator only; never copied to model workdirs."})
    manifest = {"model": "deepseek-flash", "budget": 40, "timeout_seconds": 420,
                "product_sha256": sha(PRODUCT/"agent_loop.py"),
                "semantic_sha256": {p.name: sha(p) for p in sorted((PRODUCT/"semantic").glob("*.py"))},
                "experiment_sha256": sha(Path(__file__)), "runner_sha256": sha(Path(__file__).with_name("run.py")),
                "contract_sha256": sha(contract), "case": CASE,
                "database_sha256": {v: sha(root/v/"public/warehouse.sqlite") for v in JOBS},
                "scope": "Focused synthetic lineage diagnostic, not full C1 or scale/performance test; no platform data needed.",
                "grading": "Numeric and real-reference checks only; no required table-name list. Cause and supporting observations need human review."}
    dump(root/"manifest.json", manifest)
    print(json.dumps(verification, ensure_ascii=False), flush=True)
    if not args.run:
        return
    def launch(variant):
        out = root/variant/"result"
        out.mkdir()
        result = execute(CASE, root/variant, out, PRODUCT/".venv/bin/python", 420, expected[variant], contract=contract)
        result.update(export_trace(out/CASE["id"]))
        result["variant"] = variant
        assert sha(root/variant/"public/warehouse.sqlite") == manifest["database_sha256"][variant]
        assert sha(out/CASE["id"]/"agent_loop.py") == manifest["product_sha256"]
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, JOBS))
    assert sha(PRODUCT/"agent_loop.py") == manifest["product_sha256"]
    for name, digest in manifest["semantic_sha256"].items():
        assert sha(PRODUCT/"semantic"/name) == digest
    dump(root/"summary.json", results)
    print("Results: " + str(root/"summary.json"), flush=True)


if __name__ == "__main__":
    main()
