"""执行 Plan: 跑各节点 -> 按公共维度对齐(aggregate-then-align) -> 算派生指标。

P1: SQLite; 派生在 Python 侧算(除零返回 None -> 展示"不适用")。
"""

import ast
import sqlite3

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
            ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
            ast.Mod, ast.Pow, ast.USub, ast.UAdd)


def _run_sql(sql: str, db_path: str):
    conn = sqlite3.connect(db_path, timeout=3)
    conn.execute("PRAGMA query_only=ON")
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()
    finally:
        conn.close()


def _run_mcp(node, mcp_bridge):
    """执行 MCP 节点: 调平台工具 → Python 侧分组聚合。

    产出与 _run_sql 同构 (cols, rows_as_tuple) —— 引擎后续对齐/派生零改动。
    维度分组一律 Python 侧做(工具参数只透传时间窗等, 不做分组)。
    时间列统一映射: 平台侧 'date' → 本地节点的 'day'(按天对齐才能拼上)。
    """
    if mcp_bridge is None:
        raise RuntimeError(f"模型 {node.model} 需要 MCP provider, 但 bridge 未连接")
    raw = mcp_bridge.call(node.mcp_call["tool"], node.mcp_call["args"])
    if not isinstance(raw, dict) or ("error" not in raw and "daily" not in raw):
        return {"error": "provider_error",
                "reason": "平台工具返回结构异常(非 daily 明细)"}
    if "error" in raw:
        err, note = raw.get("error"), raw.get("note") or ""
        # no_data = 该区间平台无数据(合法缺数); 其余 = provider 故障
        if err == "no_data":
            return {"error": "provider_no_data", "reason": note or "该区间平台数据缺失"}
        return {"error": "provider_error", "reason": note or f"平台工具错误: {err}"}
    rows = raw.get("daily") or []
    # 平台侧行的键是 date/source; 节点侧目标键是 day/source → 反查用原键
    rename = {"date": "day"}
    source_key = {v: k for k, v in rename.items()}   # day -> date
    group_dims = [d for d in node.output_keys if d != node.value_column]
    agg = {}
    for r in rows:
        key = tuple(r.get(source_key.get(d, d)) for d in group_dims)
        agg[key] = agg.get(key, 0) + (r.get(node.value_column) or 0)
    # 产出列 = 分组维度 + measure 别名(与 sqlite 节点的 m_? 同构, 派生表达式才能对上)
    cols = [rename.get(d, d) for d in group_dims] + [node.measure_alias]
    out = [dict(zip(group_dims, k)) | {node.value_column: v} for k, v in agg.items()]
    out = [{rename.get(k, k): v for k, v in r.items()} for r in out]
    for r in out:
        r[node.measure_alias] = r.pop(node.value_column)
    out.sort(key=lambda r: tuple(r[d] for d in group_dims))
    return cols, [tuple(r[c] for c in cols) for r in out]


def _safe_eval(expr: str, env: dict):
    """求值; 返回 (value, reason)。reason ∈ None/zero_division/missing_operand。"""
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED):
            raise ValueError(f"非法表达式节点: {type(n).__name__}")
    why = {"r": None}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if v is None:
                why["r"] = "missing_operand"
                return None
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BinOp):
            l, r = ev(node.left), ev(node.right)
            if l is None or r is None:
                why["r"] = "missing_operand"
                return None
            if isinstance(node.op, ast.Add):
                return l + r
            if isinstance(node.op, ast.Sub):
                return l - r
            if isinstance(node.op, ast.Mult):
                return l * r
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                if r == 0:
                    why["r"] = "zero_division"
                    return None
                return l / r
            if isinstance(node.op, ast.Mod):
                if r == 0:
                    why["r"] = "zero_division"
                    return None
                return l % r
        raise ValueError("不支持的表达式")

    return ev(tree), why["r"]


def _align(node_results, align_on):
    """aggregate-then-align: 各节点结果按公共维度拼。

    两遍法: 第一遍扫全部节点建"骨架"(键并集 × 全部数值列, 值=None);
    第二遍用各节点实际行填值。某键在某节点缺行 → 自然留 None(缺数, 不扔行)。
    键序 = 首个产出该键的节点顺序。
    """
    value_cols = []
    for node, _cols, _rows in node_results:
        for c in node.output_keys:
            if c not in align_on and c not in value_cols:
                value_cols.append(c)

    skeleton, order = {}, []
    for _node, _cols, rows in node_results:          # 第一遍: 键并集
        for row in rows:
            key = tuple(row.get(k) for k in align_on)
            if key not in skeleton:
                skeleton[key] = {c: None for c in value_cols}
                order.append(key)
    for _node, _cols, rows in node_results:          # 第二遍: 填值
        for row in rows:
            key = tuple(row.get(k) for k in align_on)
            for k, v in row.items():
                if k not in align_on:
                    skeleton[key][k] = v
    return [dict(zip(align_on, k)) | skeleton[k] for k in order]


_REASONS = {"zero_division": "分母为0(基期/当期无单, 比率不适用)",
            "missing_operand": "操作数缺失(该期无数据或数据源缺数)"}


def run(plan, db_path: str, mcp_bridge=None) -> dict:
    node_results = []
    for node in plan.nodes:
        if node.provider == "platform_mcp":
            res = _run_mcp(node, mcp_bridge)
            if isinstance(res, dict) and "error" in res:
                # provider 缺数据: 该节点产出空行(合法缺数), 由对齐层留空, 派生标"不适用"
                node_results.append((node, [], []))
                plan.provenance.append({"provider": node.provider, "model": node.model,
                                        "error": res["error"], "reason": res["reason"]})
                continue
            cols, rows = res
        else:
            cols, rows = _run_sql(node.sql, db_path)
        node_results.append((node, cols, [dict(zip(cols, r)) for r in rows]))

    if not node_results:
        merged = []
    elif len(node_results) == 1:
        merged = node_results[0][2]
    else:
        merged = _align(node_results, plan.align_on)

    out = []
    undefined_cells = []
    for row in merged:
        rec = {d: row.get(d) for d in plan.output_dims}
        for dv in plan.derived:
            v, why = _safe_eval(dv["expr"], row)
            rec[dv["name"]] = v
            if v is None:
                undefined_cells.append({"cell": dv["name"],
                                        "reason": why or "missing_operand"})
        out.append(rec)

    for ob in reversed(plan.order_by):
        f, desc = ob["field"], ob.get("dir", "asc").lower() == "desc"
        out.sort(key=lambda r: (r.get(f) is None, r.get(f)), reverse=desc)
    if plan.limit:
        out = out[: int(plan.limit)]

    columns = list(plan.output_dims) + [d["name"] for d in plan.derived]
    # 三态判定:
    #   out 为空                  → empty(空集)
    #   有行但数值全缺失, 且存在 provider 缺数 → no_data(数据缺失, 禁给数字)
    #   其余                      → ok(含"部分格子不适用"的正常情况)
    provider_err = [p for p in plan.provenance if p.get("error")]
    has_value = any(v is not None for r in out for v in r.values()) if out else False
    if not out:
        status = "empty"
    elif has_value:
        status = "ok"
    elif provider_err:
        status = "no_data"
    else:
        status = "no_data"   # 有行但全不适用: 多半也是某源缺数, 如实说缺
    return {
        "status": status,
        "columns": columns,
        "rows": out,
        "n_rows": len(out),
        "node_sql": [n.sql for n in plan.nodes],
        "provenance": [{"provider": n.provider, "model": n.model} for n in plan.nodes],
        "undefined_cells": sorted({u["cell"] for u in undefined_cells}) or [],
        "undefined_reasons": sorted({u["reason"] for u in undefined_cells}) or [],
        "undefined_note": ("值为 null = 不适用; 成因: "
                          + "；".join(_REASONS[r] for r in {u["reason"] for u in undefined_cells})
                          + "。如实转述'不适用', 禁止写 0 或编数") if undefined_cells else None,
    }
