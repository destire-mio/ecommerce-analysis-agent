"""P1 确定性验收(不调用 LLM): QuerySpec -> 规划 -> 执行, 断言结果。

跑法: .venv/bin/python tools/run_semantic_acceptance.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import registry, planner, engine  # noqa: E402

MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")

NEAR = {"start": "2026-09-06", "end": "2026-09-13"}


def run(reg, spec):
    plan = planner.Planner(reg).plan(spec)
    return engine.run(plan, DB)


def approx(a, b, tol=0.05):
    return a is not None and abs(float(a) - float(b)) <= tol


def main():
    reg = registry.load(MDL, db_path=DB)
    checks, failed = [], 0

    def check(name, ok, detail=""):
        nonlocal failed
        checks.append((name, ok, detail))
        if not ok:
            failed += 1

    # 1. 单指标: GMV
    r = run(reg, {"metrics": ["GMV"], "time_range": NEAR})
    check("GMV 总 = 800000", r["rows"] and approx(r["rows"][0]["GMV"], 800000),
          str(r["rows"]))

    # 2. 单指标: 支付订单数
    r = run(reg, {"metrics": ["支付订单数"], "time_range": NEAR})
    check("支付订单数 = 8000", r["rows"] and approx(r["rows"][0]["支付订单数"], 8000),
          str(r["rows"]))

    # 3. 比率: 单均金额
    r = run(reg, {"metrics": ["单均金额"], "time_range": NEAR})
    check("单均金额 = 100", r["rows"] and approx(r["rows"][0]["单均金额"], 100),
          str(r["rows"]))

    # 4. 多指标 + 分组
    r = run(reg, {"metrics": ["GMV", "支付订单数", "单均金额"],
                  "dimensions": ["category"], "time_range": NEAR})
    by = {row["category"]: row for row in r["rows"]}
    check("家电 GMV = 350000", "家电" in by and approx(by["家电"]["GMV"], 350000), str(by))
    check("家电 订单数 = 3635", "家电" in by and approx(by["家电"]["支付订单数"], 3635), str(by))
    check("家电 单均 ≈ 96.3", "家电" in by and approx(by["家电"]["单均金额"], 96.3), str(by))
    check("其他行业 GMV = 450000", "其他行业" in by and approx(by["其他行业"]["GMV"], 450000), str(by))

    # 5. 时间粒度 day
    r = run(reg, {"metrics": ["GMV"], "time_grain": "day", "time_range": NEAR})
    check("按天 7 行", r["n_rows"] == 7, f"n_rows={r['n_rows']}")

    # 6. 排序 + limit
    r = run(reg, {"metrics": ["GMV"], "dimensions": ["category"], "time_range": NEAR,
                  "order_by": [{"field": "GMV", "dir": "desc"}], "limit": 1})
    check("按 GMV 降序取 1", r["n_rows"] == 1 and r["rows"][0]["category"] == "其他行业",
          str(r["rows"]))

    # 7. 未开放指标必须被挡
    try:
        run(reg, {"metrics": ["退款率"], "time_range": NEAR})
        check("退款率被挡", False, "未报错")
    except planner.PlanError as e:
        check("退款率被挡", e.payload.get("error") == "unavailable_metric", str(e.payload))

    # 8. 未知指标必须被挡
    try:
        run(reg, {"metrics": ["不存在"], "time_range": NEAR})
        check("未知指标被挡", False, "未报错")
    except planner.PlanError as e:
        check("未知指标被挡", e.payload.get("error") == "unknown_metric", str(e.payload))

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    print(f"{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
