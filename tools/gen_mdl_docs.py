"""从 mdl.yaml 单向生成人类可读文档（单一事实源: 只此一处手写）。

跑法: .venv/bin/python tools/gen_mdl_docs.py
输出: data/ecommerce/语义层说明.md
"""

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from semantic import registry  # noqa: E402

MDL = os.path.join(ROOT, "data", "ecommerce", "mdl.yaml")
OUT = os.path.join(ROOT, "data", "ecommerce", "语义层说明.md")


def gen(reg: registry.Registry) -> str:
    L = []
    L.append("# 语义层说明（由 `mdl.yaml` 自动生成，请勿手改）")
    L.append("")
    L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 唯一手写处：`mdl.yaml`")
    L.append("")
    L.append("## 模型")
    for name, m in reg.models.items():
        L.append(f"### {name}")
        L.append(f"- 说明：{m.get('description', '')}")
        L.append(f"- 粒度：{m['grain']} ｜ 主键：{', '.join(m['key'])} ｜ 数据源：{m['provider']}")
        L.append(f"- 维度：{', '.join(m.get('dimensions', [])) or '（无）'}")
        L.append("- 列：" + "、".join(f"{c['name']}({c['unit']})" for c in m["columns"]))
        L.append("")
    L.append("## 度量（Measure）")
    for name, s in reg.measures.items():
        agg = f"{s['agg']}({s.get('column') or s.get('distinct', '*')})"
        L.append(f"- **{name}**：{agg} ｜ 单位 {s['unit']} ｜ 可加性 {s.get('additivity', '')}")
        L.append(f"  - {s.get('description', '')}")
    L.append("")
    L.append("## 指标（Metric）")
    for name, mt in reg.metrics.items():
        L.append(f"- **{name}**（{mt['unit']}）：{mt.get('description', '')}"
                 f"　← measure `{mt['measure']}`")
    L.append("")
    L.append("## 比率（Ratio）")
    for name, r in reg.ratios.items():
        L.append(f"- **{name}**（{r['unit']}）= `{r['expr']}`")
        L.append(f"  - {r.get('description', '')}")
    L.append("")
    L.append("## 未开放指标（禁止给数字）")
    for name, u in reg.unavailable.items():
        L.append(f"- **{name}**：{u['reason']}")
    L.append("")
    return "\n".join(L)


def main():
    reg = registry.load(MDL)
    text = gen(reg)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"written: {OUT} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
