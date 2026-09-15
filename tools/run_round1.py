"""第 1 轮验收: 跑 MOCK/REAL/FAIL 三条路径, 提取结果, 统计成功/失败个数.

用法: python3 tools/run_round1.py
输出: results/round1.json
"""

import json
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent.py")
OUT = os.path.join(ROOT, "results", "round1.json")


def run(mode: str) -> dict:
    p = subprocess.run([sys.executable, AGENT, "--mode", mode],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    entry = {"mode": mode.upper(), "exit": p.returncode, "status": "ok" if p.returncode == 0 else "error"}
    try:
        payload = json.loads(p.stdout.split("\n", 1)[1]) if "\n" in p.stdout else {}
        entry["sql"] = payload.get("sql")
        rows = payload.get("result")
        if rows is not None:
            entry["n_rows"] = len(rows)
            entry["sample"] = rows[:3]
        entry["error"] = payload.get("error")
    except Exception:
        entry["raw"] = (p.stdout + p.stderr)[-200:]
    return entry


def main() -> None:
    results = [run(m) for m in ("mock", "real", "fail")]
    n_ok = sum(r["status"] == "ok" for r in results)
    n_err = len(results) - n_ok
    summary = {"n_total": len(results), "n_ok": n_ok, "n_error": n_err, "results": results}
    json.dump(summary, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"成功 {n_ok} / 失败 {n_err} (共 {len(results)}) -> {OUT}")
    for r in results:
        mark = "OK  " if r["status"] == "ok" else "FAIL"
        print(f"  [{mark}] {r['mode']}: {r.get('error') or r.get('sql')}")


if __name__ == "__main__":
    main()
