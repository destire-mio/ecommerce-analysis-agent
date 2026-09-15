"""A2 compare 确定性验收(不调用 LLM)。"""

import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import engine, planner, registry  # noqa: E402

MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")
PLATFORM_DB = os.path.join(ROOT, "data", "platform", "platform_data.sqlite")
CURR = {"start": "2026-09-06", "end": "2026-09-13"}
PREV = {"start": "2026-08-30", "end": "2026-09-06"}


class FakeMcp:
    """验收用最小 bridge，接口与 McpBridge.call 相同。"""

    def call(self, tool, args):
        if tool != "get_shop_traffic":
            raise AssertionError(f"unexpected MCP tool: {tool}")
        conn = sqlite3.connect(PLATFORM_DB)
        try:
            rows = conn.execute(
                "SELECT date, source, visitors FROM shop_traffic "
                "WHERE date >= ? AND date < ? ORDER BY date, source",
                (args.get("start"), args.get("end")),
            ).fetchall()
        finally:
            conn.close()
        return {"daily": [{"date": d, "source": s, "visitors": v}
                           for d, s, v in rows]}


def approx(value, expected, tol=0.01):
    return value is not None and abs(float(value) - expected) < tol


def main():
    reg = registry.load(MDL, db_path=DB)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    total = engine.run(planner.Planner(reg).plan({
        "metrics": ["GMV"], "time_range": CURR, "compare": PREV,
    }), DB)
    row = total["rows"][0]
    check("GMV 总量 curr/prev/delta/pct",
          row["GMV"] == 800000 and row["GMV_prev"] == 1000000
          and row["GMV_delta"] == -200000 and approx(row["GMV_pct"], -20.0),
          str(total))

    by_category = engine.run(planner.Planner(reg).plan({
        "metrics": ["GMV"], "dimensions": ["category"],
        "time_range": CURR, "compare": PREV,
    }), DB)
    rows = {r["category"]: r for r in by_category["rows"]}
    check("GMV 按品类比较",
          rows.get("家电", {}).get("GMV_delta") == -250000
          and approx(rows.get("家电", {}).get("GMV_pct"), -41.6667)
          and rows.get("其他行业", {}).get("GMV_delta") == 50000
          and approx(rows.get("其他行业", {}).get("GMV_pct"), 12.5),
          str(rows))

    visitors = engine.run(planner.Planner(reg).plan({
        "metrics": ["访客数"], "time_range": CURR, "compare": PREV,
    }), DB, mcp_bridge=FakeMcp())
    vrow = visitors["rows"][0]
    check("跨源访客数比较",
          vrow["访客数"] == 110596 and vrow["访客数_prev"] == 98975
          and vrow["访客数_delta"] == 11621
          and approx(vrow["访客数_pct"], 11.7413),
          str(visitors))

    # 造一个“本期有数据、基期为0”的临时库，验证 pct 不返回 inf/0。
    tmpdir = tempfile.mkdtemp(prefix="compare-acceptance-")
    try:
        zero_db = os.path.join(tmpdir, "ecommerce.sqlite")
        shutil.copy2(DB, zero_db)
        conn = sqlite3.connect(zero_db)
        conn.execute(
            "UPDATE order_items SET amount = 0 WHERE order_id IN "
            "(SELECT order_id FROM orders WHERE pay_time >= ? AND pay_time < ?)",
            (PREV["start"], PREV["end"]),
        )
        conn.commit()
        conn.close()
        zero = engine.run(planner.Planner(reg).plan({
            "metrics": ["GMV"], "time_range": CURR, "compare": PREV,
        }), zero_db)
        zrow = zero["rows"][0]
        check("基期为0时 pct=None、delta照常",
              zrow["GMV_prev"] == 0 and zrow["GMV_delta"] == 800000
              and zrow["GMV_pct"] is None,
              str(zero))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        planner.Planner(reg).plan({
            "metrics": ["GMV"], "time_range": CURR, "compare": PREV,
            "time_grain": "day",
        })
        check("compare + day 被拦截", False, "未抛出 PlanError")
    except planner.PlanError as exc:
        check("compare + day 被拦截",
              exc.payload.get("error") == "compare_with_grain", str(exc.payload))

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
