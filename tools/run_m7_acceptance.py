"""M7 治理与运维确定性验收(不调用 LLM)。"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import engine, planner, registry  # noqa: E402
import agent_loop  # noqa: E402
from agent_loop import build_material  # noqa: E402
from tools.query_audit import read_audit  # noqa: E402

MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")
NEAR = {"start": "2026-09-06", "end": "2026-09-13"}


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    reg = registry.load(MDL, db_path=DB)
    sales_ref = reg.models["fct_sales_line"]["ref_sql"]
    orders_ref = reg.models["fct_orders"]["ref_sql"]
    check("销售模型移除硬编码 shop_id=1", "shop_id = 1" not in sales_ref.lower(), sales_ref)
    check("订单模型移除硬编码 shop_id=1", "shop_id = 1" not in orders_ref.lower(), orders_ref)
    check("scope 列声明且隐藏",
          reg.models["fct_sales_line"].get("scope") == {"column": "shop_id"}
          and reg.is_hidden("fct_sales_line", "shop_id"),
          str(reg.models["fct_sales_line"].get("scope")))

    plan = planner.Planner(reg).plan({"metrics": ["GMV"], "time_range": NEAR}, tenant=2)
    check("tenant=2 注入节点 SQL", "shop_id = 2" in plan.nodes[0].sql, plan.nodes[0].sql)
    order_plan = planner.Planner(reg).plan(
        {"metrics": ["订单数订单粒度"], "time_range": NEAR}, tenant=2)
    check("订单模型也注入 tenant", "shop_id = 2" in order_plan.nodes[0].sql,
          order_plan.nodes[0].sql)

    result = engine.run(
        planner.Planner(reg).plan({"metrics": ["GMV"], "time_range": NEAR}, tenant=1),
        DB,
    )
    check("tenant=1 GMV 锚点不变",
          result["rows"] and result["rows"][0]["GMV"] == 800000,
          str(result))
    check("执行输出不暴露 hidden 列", "shop_id" not in result["columns"], str(result))

    description = registry.describe(reg)
    check("材料索引不列出 hidden shop_id", "shop_id" not in description, description)
    material = build_material(DB, "ecommerce", os.path.join(ROOT, "data", "ecommerce", "skills"))
    check("材料包不列出 hidden shop_id", "shop_id" not in material, material)
    try:
        planner.Planner(reg).plan({
            "metrics": ["GMV"], "dimensions": ["shop_id"], "time_range": NEAR,
        }, tenant=1)
        check("请求 hidden 列被拦", False, "未抛出 PlanError")
    except planner.PlanError as exc:
        check("请求 hidden 列被拦",
              exc.payload.get("error") == "column_hidden", str(exc.payload))

    # 在隔离临时目录用假的 LLM 响应跑一题真实 run_agent，验证审计不是只测 helper。
    class _Function:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class _ToolCall:
        def __init__(self, name, arguments):
            self.id, self.function = "m7-tc", _Function(name, arguments)

        def model_dump(self):
            return {"id": self.id, "type": "function",
                    "function": {"name": self.function.name,
                                 "arguments": self.function.arguments}}

    class _Message:
        def __init__(self, content=None, tool_calls=None):
            self.content, self.tool_calls = content, tool_calls

    class _Response:
        def __init__(self, message):
            self.choices = [type("_Choice", (), {"message": message})()]
            self.usage = None

    class _FakeClient:
        def __init__(self):
            self.calls = 0
            self.chat = self
            self.completions = self

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                spec = {"metrics": ["GMV"], "time_range": NEAR}
                return _Response(_Message(tool_calls=[_ToolCall(
                    "query_metrics", json.dumps(spec, ensure_ascii=False))]))
            return _Response(_Message(
                "近7天商品支付金额是 800000 元 (Q1)\n###EVIDENCE###\nQ1: 语义查询"))

    old_cwd = os.getcwd()
    old_bench_dir = agent_loop.BENCH_DIRS["ecommerce"]
    old_openai = agent_loop.OpenAI
    tmpdir = tempfile.mkdtemp(prefix="m7-audit-")
    try:
        os.chdir(tmpdir)
        agent_loop.BENCH_DIRS["ecommerce"] = os.path.dirname(DB)
        fake = _FakeClient()
        agent_loop.OpenAI = lambda **_kwargs: fake
        run = agent_loop.run_agent(
            "M7 验收问题", "ecommerce", "ecommerce", tenant=1,
            skills_dir=os.path.join(ROOT, "data", "ecommerce", "skills"))
        audit_path = os.path.join(tmpdir, "runs", "audit.jsonl")
        records = read_audit(audit_path, session=run.get("session_id"))
        record = records[-1] if records else {}
        check("真实 run_agent 产生审计", run.get("status") == "ok"
              and record.get("question") == "M7 验收问题"
              and record.get("tools_used") == ["query_metrics"]
              and record.get("status") == "ok", json.dumps(record, ensure_ascii=False))
        check("审计文件可按 session 过滤",
              len(records) == 1 and not read_audit(audit_path, session="other"),
              str(records))
        check("审计文件可按日期过滤",
              len(read_audit(audit_path, day=record.get("ts", "")[:10])) == 1,
              str(record))
    finally:
        agent_loop.OpenAI = old_openai
        agent_loop.BENCH_DIRS["ecommerce"] = old_bench_dir
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
