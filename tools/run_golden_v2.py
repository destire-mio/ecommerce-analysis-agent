"""归因场景 Golden Cases v2 批跑: 逐题跑 agent, 机械预填期望检查, 出人工判定答卷.

用法: .venv/bin/python tools/run_golden_v2.py [--file golden_cases_s3.json] [case_id ...]
  不带参数跑 data/ecommerce/golden_cases_v2.json 全部; --file 换题库(结果写 results/golden_<后缀>/)。
每题独立写结果 JSON, 人工判定在结果文件中查看。
"""

import json
import os
import re
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "data", "ecommerce", "golden_cases_v2.json")
OUT_DIR = os.path.join(ROOT, "results", "golden_v2")


def run_case(case: dict, last_session: str = None) -> dict:
    session = None
    if case.get("session") == "continue" and last_session:
        session = last_session
    argv = [sys.executable, os.path.join(ROOT, "agent_loop.py"),
            "--question", case["question"], "--db", "ecommerce", "--bench", "ecommerce"]
    if session:
        argv += ["--session", session]
    p = subprocess.run(argv, capture_output=True, text=True, cwd=ROOT, timeout=420)
    lines = p.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else {"status": "crash"}
    except json.JSONDecodeError:
        out = {"status": "crash", "answer": (p.stdout + p.stderr)[-300:]}
    out["_session"] = out.get("session_id") or last_session
    return out


def check(case: dict, out: dict) -> dict:
    exp = case["expect"]
    answer = out.get("answer", "") or ""
    full = (out.get("answer_full") or "") + answer
    results = {}

    # 数字检查
    norm = full.replace(",", "").replace("，", "")
    for n in exp.get("numbers", []):
        results[f"数字 {n}"] = str(n) in norm

    # 结论关键词(字符串=必须含; 列表=任选其一)
    for kw in exp.get("conclusion_must_contain", []):
        if isinstance(kw, list):
            results[f"含{'/'.join(kw)}"] = any(w in full for w in kw)
        else:
            results[f"含'{kw}'"] = kw in full
    for kw in exp.get("must_say", []):
        if isinstance(kw, list):
            results[f"说明{'/'.join(kw)[:16]}…"] = any(w in full for w in kw)
        else:
            results[f"说明'{kw[:12]}…'"] = kw in full
    for kw in exp.get("must_not_contain", []):
        results[f"禁止'{kw}'"] = kw not in norm

    # 必调工具: 从 trace 里查
    trace_path = os.path.join(ROOT, "runs", out.get("run_id", ""), "trace.jsonl")
    trace_text = open(trace_path).read() if os.path.exists(trace_path) else ""
    for tool in exp.get("must_call_tools", []):
        results[f"调用了 {tool}"] = f'"{tool}"' in trace_text

    passed = all(results.values()) and out.get("status") == "ok"
    return {"passed": passed, "checks": results, "out": out}


def main():
    args = sys.argv[1:]
    case_file, out_dir = CASES, OUT_DIR
    if "--file" in args:
        fn = args[args.index("--file") + 1]
        case_file = os.path.join(ROOT, "data", "ecommerce", fn)
        stem = re.sub(r"^golden_cases_|\.json$", "", fn)
        out_dir = os.path.join(ROOT, "results", f"golden_{stem}")
    os.makedirs(out_dir, exist_ok=True)
    cases = json.load(open(case_file))["cases"]
    file_val = args[args.index("--file") + 1] if "--file" in args else None
    only = {a for a in args if not a.startswith("--") and a != file_val}
    last_session = None
    for case in cases:
        if only and case["id"] not in only:
            continue
        print(f"=== {case['id']}: {case['question']} ===", flush=True)
        out = run_case(case, last_session)
        last_session = out.get("_session")
        verdict = check(case, out)
        result = {"case_id": case["id"], "question": case["question"],
                  "status": out.get("status"), "answer": out.get("answer"),
                  "answer_full": out.get("answer_full"), "executions": out.get("executions"),
                  "checks": verdict["checks"], "passed": verdict["passed"]}
        json.dump(result, open(os.path.join(out_dir, f"{case['id']}.json"), "w"),
                  ensure_ascii=False, indent=1, default=str)
        print(f"  [{ 'PASS' if verdict['passed'] else 'FAIL' }] status={out.get('status')}")
        for k, v in verdict["checks"].items():
            print(f"    {'✓' if v else '✗'} {k}")
        print()


if __name__ == "__main__":
    main()
