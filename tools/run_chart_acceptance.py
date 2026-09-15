"""S4 图表确定性验收(不调用 LLM)。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent_loop import chart  # noqa: E402

DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")
SPEC = {
    "metrics": ["GMV"],
    "dimensions": ["category"],
    "time_range": {"start": "2026-09-06", "end": "2026-09-13"},
}


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    bar = chart(SPEC, "bar", "category", "GMV", DB)
    check("bar option 与文件",
          bar.get("status") == "saved"
          and bar["option"]["series"][0]["type"] == "bar"
          and set(bar["option"]["xAxis"]["data"]) >= {"家电", "其他行业"}
          and set(bar["option"]["series"][0]["data"]) >= {350000, 450000}
          and os.path.exists(bar["path"]),
          str(bar))

    line = chart(SPEC, "line", "category", "GMV", DB)
    check("line option", line.get("status") == "saved"
          and line["option"]["series"][0]["type"] == "line",
          str(line))

    pie = chart(SPEC, "pie", "category", "GMV", DB)
    pie_data = pie.get("option", {}).get("series", [{}])[0].get("data", [])
    check("pie option", pie.get("status") == "saved"
          and pie["option"]["series"][0]["type"] == "pie"
          and {item.get("name") for item in pie_data} >= {"家电", "其他行业"}
          and {item.get("value") for item in pie_data} >= {350000, 450000},
          str(pie))

    bad = chart(SPEC, "scatter", "category", "GMV", DB)
    check("未知图表类型被拦截", bad.get("error") == "unknown_chart_type", str(bad))

    if bar.get("path") and os.path.exists(bar["path"]):
        with open(bar["path"], encoding="utf-8") as fh:
            check("落盘 JSON 可读取", json.load(fh) == bar["option"])

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
