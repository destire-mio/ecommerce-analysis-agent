"""M6/M7 发布门禁：构建、确定性验收、Golden 与 Web 冒烟。"""

import argparse
import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


DETERMINISTIC = [
    ("semantic", "tools/run_semantic_acceptance.py"),
    ("compare", "tools/run_compare_acceptance.py"),
    ("lmdi", "tools/run_lmdi_acceptance.py"),
    ("s3", "tools/run_s3_acceptance.py"),
    ("chart", "tools/run_chart_acceptance.py"),
    ("report", "tools/run_report_acceptance.py"),
    ("m7", "tools/run_m7_acceptance.py"),
]
GOLDENS = [
    "golden_cases_v2.json",
    "golden_cases_s3.json",
    "golden_cases_m3.json",
    "golden_cases_a2.json",
    "golden_cases_m4.json",
]


def _run(label, command, cwd=ROOT, reject_markers=(), timeout=900):
    print(f"\n=== {label} ===")
    print("$ (cd " + os.path.relpath(cwd, ROOT) + " && " + " ".join(command) + ")")
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[FAIL] {exc}")
        return False
    output = (completed.stdout or "") + (completed.stderr or "")
    print(output[-6000:], end="" if output.endswith("\n") else "\n")
    ok = completed.returncode == 0
    rejected = [marker for marker in reject_markers if marker in output]
    if rejected:
        ok = False
        print(f"[FAIL] 输出包含失败标记: {', '.join(rejected)}")
    if not ok and completed.returncode != 0:
        print(f"[FAIL] exit={completed.returncode}")
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行 M6/M7 发布门禁")
    parser.add_argument("--skip-llm", action="store_true",
                        help="跳过 Golden 与 Web /ask 冒烟，仅跑本地确定性门禁")
    parser.add_argument("--golden-timeout", type=int, default=3600,
                        help="每个 Golden 文件的最大秒数，默认 3600")
    args = parser.parse_args(argv)

    if not os.path.exists(PYTHON):
        print(f"[FAIL] 找不到 {PYTHON}")
        return 1

    results = []
    npm = ["npm", "run", "build"]
    if not os.path.isdir(os.path.join(ROOT, "frontend", "node_modules")):
        results.append(("npm install", _run(
            "npm install", ["npm", "install", "--no-audit", "--no-fund"], cwd=os.path.join(ROOT, "frontend"))))
    results.append(("frontend build", _run(
        "frontend build", npm, cwd=os.path.join(ROOT, "frontend"))))

    for label, script in DETERMINISTIC:
        if os.path.exists(os.path.join(ROOT, script)):
            results.append((label, _run(label, [PYTHON, script])))
        else:
            results.append((label, False))
            print(f"\n=== {label} ===\n[FAIL] 文件不存在: {script}")

    if not args.skip_llm:
        for filename in GOLDENS:
            path = os.path.join(ROOT, "data", "ecommerce", filename)
            if not os.path.exists(path):
                continue
            label = f"golden:{filename}"
            ok = _run(label, [PYTHON, "tools/run_golden_v2.py", "--file", filename],
                      reject_markers=("[FAIL]",), timeout=args.golden_timeout)
            results.append((label, ok))
        results.append(("web smoke", _run("web smoke", [PYTHON, "tools/run_smoke.py"])))
    else:
        print("\n[INFO] --skip-llm：已跳过 Golden 与 Web /ask 冒烟")

    print("\n" + "=" * 60)
    for label, ok in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print("=" * 60)
    passed = sum(ok for _, ok in results)
    print(f"{passed}/{len(results)} passed")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
