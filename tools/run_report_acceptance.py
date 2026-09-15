"""S5 窄版报告确定性验收(不调用 LLM)。"""

import os
import sys

from docx import Document

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent_loop import run_query_metrics, save_report  # noqa: E402

DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")
SPEC = {
    "metrics": ["GMV"],
    "time_range": {"start": "2026-09-06", "end": "2026-09-13"},
}


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    query = run_query_metrics(SPEC, DB)
    row = query["rows"][0]
    check("Q1 真数为800000", row.get("GMV") == 800000, str(query))
    state = {
        "session_id": "s5-acceptance",
        "db_path": DB,
        "data_cutoff": "2026-09-13",
        "executions": {
            "Q1": {
                "tool": "query_metrics",
                "args": SPEC,
                "sql": query.get("sql"),
                "columns": query.get("columns"),
                "rows": query.get("rows"),
                "n_rows": query.get("n_rows"),
            }
        },
    }
    content = (
        "## 一、问题\n近7天商品支付金额是多少？\n"
        "## 二、数据及结果\n商品支付金额为 800000 元 (Q1)\n"
        "## 三、原因\n本次仅回答已查询的指标。\n"
        "## 四、改进建议\n继续观察下一周期。\n"
        "###EVIDENCE###\nQ1: 语义层查询结果。"
    )
    result = save_report("S5 窄版验收", content, state)
    check("save_report 通过并落盘", result.get("status") == "saved"
          and os.path.exists(result.get("path", "")), str(result))
    if result.get("path") and os.path.exists(result["path"]):
        text = "\n".join(p.text for p in Document(result["path"]).paragraphs)
        check("Word 末尾含自动口径节",
              "口径说明（由语义层生成）" in text
              and "负责人：业务负责人(演示)" in text
              and "版本：v1" in text,
              text)
    else:
        check("Word 末尾含自动口径节", False, "Word 未生成")

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
