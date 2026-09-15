"""M4 LMDI 确定性验收(不调用 LLM)。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import registry  # noqa: E402
from semantic.formulas import lmdi  # noqa: E402


MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
DB = os.path.join(ROOT, "data", "ecommerce", "ecommerce.sqlite")

PREV = {
    "total": 1_000_000,
    "visitors": 98_975,
    "conversion": 0.10103561505430664,
    "average": 100.0,
}
CURR = {
    "total": 800_000,
    "visitors": 110_596,
    "conversion": 0.0723353466671489,
    "average": 100.0,
}


def factors(curr=None, prev=None):
    curr = curr or CURR
    prev = prev or PREV
    return {
        "访客数": {"curr": curr["visitors"], "prev": prev["visitors"]},
        "转化率": {"curr": curr["conversion"], "prev": prev["conversion"]},
        "单均金额": {"curr": curr["average"], "prev": prev["average"]},
    }


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    reg = registry.load(MDL, db_path=DB)
    check("MDL 恒等式已加载", "GMV乘法恒等" in reg.identities)
    check("MDL 对账规则已加载", "GMV口径对账" in reg.reconciliations)

    result = lmdi(CURR["total"], PREV["total"], factors())
    contributions = result["contributions"]
    check("ΔGMV = -200000", result["total_delta"] == -200_000, str(result))
    check("访客贡献", abs(contributions["访客数"] - 99_502.4310) < 0.01,
          str(contributions))
    check("转化率贡献", abs(contributions["转化率"] - (-299_502.4310)) < 0.01,
          str(contributions))
    check("客单价贡献 = 0", abs(contributions["单均金额"]) < 0.01,
          str(contributions))
    check("closure < 1e-3", abs(result["closure"]) < 1e-3, str(result["closure"]))
    check("同源重建相对误差 < 1e-6",
          result["reconciliation"]["rel_error"] < 1e-6,
          str(result["reconciliation"]))

    undefined_total = lmdi(800_000, 0, factors())
    check("总量0 = undefined", undefined_total["status"] == "undefined",
          str(undefined_total))

    zero_prev = factors()
    zero_prev["转化率"]["prev"] = 0
    zero_factor = lmdi(CURR["total"], PREV["total"], zero_prev)
    check("单因素0贡献为 None", zero_factor["contributions"]["转化率"] is None,
          str(zero_factor))
    check("单因素0有 residual", "residual" in zero_factor,
          str(zero_factor))
    check("单因素0大残差有 warning",
          abs(zero_factor["residual"]) > 0.5 * abs(zero_factor["total_delta"])
          and zero_factor["warnings"],
          str(zero_factor))

    mismatch = factors()
    mismatch["转化率"]["curr"] *= 1.1
    mismatch_result = lmdi(CURR["total"], PREV["total"], mismatch, tolerance=0.02)
    check("超过2%对账带 warning",
          mismatch_result["reconciliation"].get("warning") is not None,
          str(mismatch_result["reconciliation"]))

    equal_factor = lmdi(CURR["total"], CURR["total"], factors(CURR, CURR), tolerance=0.02)
    # 单均金额在锚点两期相同；在真实下降案例中它的贡献应为 0。
    check("因素比=1贡献为0",
          result["contributions"]["单均金额"] == 0.0,
          str(result["contributions"]))
    # 也覆盖总量相等时的对数均值连续极限，避免 0/0。
    check("总量相等不抛异常", equal_factor["status"] == "ok",
          str(equal_factor))

    print("=" * 60)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  <- {detail}"))
    print("=" * 60)
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} passed")
    return 1 if passed != len(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
