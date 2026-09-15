"""第 3 轮: 判分器 + 批跑器.

判分规则 B: 行集合比对——两边行数相同且逐行对得上(忽略行序)才算对.
批跑: 一次全量, 每题调 Runtime, 结果与失败归因写 results/exam_*.json.

用法: python3 tools/run_exam.py [--exam data/exam_bird.json] [--out results/exam_bird.json]
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIRS = {
    "spider": os.path.join(ROOT, "data", "spider_data", "database"),
    "bird": os.path.join(ROOT, "data", "minidev", "MINIDEV", "dev_databases"),
}
DEFAULT_EXAM = os.path.join(ROOT, "data", "exam.json")
DEFAULT_OUT = os.path.join(ROOT, "results", "exam.json")


def resolve_db_path(db_id: str, bench: str = None) -> str:
    roots = [(bench, BENCH_DIRS[bench])] if bench else list(BENCH_DIRS.items())
    for _, root in roots:
        p = os.path.join(root, db_id, f"{db_id}.sqlite")
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"database not found: {db_id}")


def run_sql(sql: str, db_id: str, bench: str = None) -> list:
    import sqlite3
    conn = sqlite3.connect(resolve_db_path(db_id, bench))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _norm_cell(c) -> str:
    """格子规范化: None->占位符, 数值统一 float 精度, 数字长相的字符串按数字比."""
    if c is None:
        return "\x00NULL"
    if isinstance(c, bool):
        return str(c)
    if isinstance(c, (int, float)):
        return f"{float(c):.6f}"
    if isinstance(c, str):
        try:
            return f"{float(c):.6f}"
        except ValueError:
            return c
    return str(c)


def canon(rows: list) -> list:
    """规则 B 的规范化: 每格归一, 再整行排序 (None 安全)."""
    return sorted(tuple(_norm_cell(c) for c in r) for r in rows)


def strip_guard_limit(sql: str) -> str:
    """剥掉 Runtime 自动加的保护包装: SELECT * FROM (...) LIMIT 100000."""
    s = sql.strip().rstrip(";")
    if s.upper().startswith("SELECT * FROM (") and s.upper().endswith("LIMIT 100000"):
        inner = s[s.index("(") + 1: s.rindex(")")]
        if inner.count("(") == inner.count(")"):
            return inner.strip()
    return s


def judge(pred_sql: str, gold_sql: str, db_id: str, bench: str = None) -> dict:
    """执行比对. 返回 {correct, reason}."""
    try:
        gold_rows = canon(run_sql(gold_sql, db_id, bench))
    except Exception as e:
        return {"correct": False, "reason": f"gold_exec_error: {e}"}
    try:
        pred_rows = canon(run_sql(strip_guard_limit(pred_sql), db_id))
    except Exception as e:
        return {"correct": False, "reason": f"pred_exec_error: {e}"}
    if len(gold_rows) != len(pred_rows):
        return {"correct": False, "reason": f"row_count {len(pred_rows)} != {len(gold_rows)}"}
    if gold_rows != pred_rows:
        return {"correct": False, "reason": "row_mismatch"}
    return {"correct": True, "reason": "ok"}


def run_one(q: dict, exam_file: str) -> dict:
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, os.path.join(ROOT, "runtime.py"),
         "--exam-file", os.path.relpath(exam_file, ROOT), "--exam", str(q["id"])],
        capture_output=True, text=True, cwd=ROOT, timeout=180,
    )
    wall = round(time.time() - t0, 1)
    entry = {"id": q["id"], "level": q["level"], "db_id": q["db_id"], "wall_s": wall,
             "exit": p.returncode, "status": "error" if p.returncode != 0 else "ok"}
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
        entry["sql"] = out.get("sql")
        entry["n_rows"] = out.get("n_rows")
        if out.get("status") != "ok":
            entry["reason"] = f"runtime_failed: {out.get('reason')}"
    except Exception:
        entry["reason"] = f"bad_output: {(p.stdout + p.stderr)[-200:]}"
    if entry.get("status") == "ok" and entry.get("sql"):
        j = judge(entry["sql"], q["gold_sql"], q["db_id"], q.get("bench"))
        entry["correct"] = j["correct"]
        entry["judge"] = j["reason"]
    else:
        entry["correct"] = False
    # LLM 调用次数: 从 trace 里数 prompt 事件
    runs = sorted(os.listdir(os.path.join(ROOT, "runs")))
    last = [r for r in runs if not r.startswith("run-test")][-1]
    trace_path = os.path.join(ROOT, "runs", last, "trace.jsonl")
    entry["llm_calls"] = sum(1 for line in open(trace_path) if '"action": "prompt"' in line)
    return entry


def main() -> None:
    argv = sys.argv
    exam_file = argv[argv.index("--exam") + 1] if "--exam" in argv else DEFAULT_EXAM
    out_path = argv[argv.index("--out") + 1] if "--out" in argv else DEFAULT_OUT
    exam = json.load(open(exam_file))
    results = []
    for q in exam:
        print(f"跑第 {q['id']} 题 (level={q['level']}, db={q['db_id']}) ...", flush=True)
        results.append(run_one(q, exam_file))
    by_level = {}
    levels = sorted({r["level"] for r in results})
    for lv in levels:
        sub = [r for r in results if r["level"] == lv]
        by_level[lv] = f"{sum(r['correct'] for r in sub)}/{len(sub)}"
    summary = {
        "n_total": len(results),
        "n_correct": sum(r["correct"] for r in results),
        "by_level": by_level,
        "total_llm_calls": sum(r["llm_calls"] for r in results),
        "total_wall_s": round(sum(r["wall_s"] for r in results), 1),
        "results": results,
    }
    json.dump(summary, open(out_path, "w"), ensure_ascii=False, indent=1)
    print(f"\n== 总分 {summary['n_correct']}/{summary['n_total']} ==")
    print(f"难度分布: {by_level}")
    print(f"LLM 调用 {summary['total_llm_calls']} 次, 总耗时 {summary['total_wall_s']}s -> {out_path}")


if __name__ == "__main__":
    main()
