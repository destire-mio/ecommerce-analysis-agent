"""确定性语义查询规划器: QuerySpec -> Plan(纯逻辑, 不碰数据库)。

九步里的前五步:
  ① 解析校验 QuerySpec  ② 拆指标 DAG  ③ 校验维度/定对齐键
  ④ 按(数据源,模型)分组建节点  ⑤ 编译为 SQL
执行/对齐/派生见 engine.py。本版(P1)只处理 provider=sqlite。
"""

from dataclasses import dataclass, field
import re

AGG_FN = {"sum": "SUM", "avg": "AVG", "max": "MAX", "min": "MIN"}


class PlanError(Exception):
    def __init__(self, error: str, **extra):
        self.payload = {"error": error, **extra}
        super().__init__(str(self.payload))


@dataclass
class AggNode:
    provider: str
    model: str
    sql: str = ""              # sqlite 节点: 一条聚合 SQL
    output_keys: list = None   # 该节点产出的列名(分组列 + measure 别名)
    mcp_call: dict = None      # mcp 节点: {tool, args}
    value_column: str = None   # mcp 节点: 聚合的数值列名
    measure_alias: str = None  # mcp 节点: 该列对应的 measure 别名(供派生对上)


@dataclass
class Plan:
    spec: dict
    nodes: list
    align_on: list             # 公共维度(对齐键)
    derived: list              # [{name, expr, unit}] —— expr 用 measure 别名表达
    output_dims: list
    order_by: list
    limit: int
    provenance: list = field(default_factory=list)


class Planner:
    def __init__(self, registry):
        self.reg = registry
        self._measure_alias = {}
        self._leaves = {}       # measure 名 -> measure 定义
        self._resolved = {}     # 指标名 -> 解析后的表达式(measure 别名)

    # ---------------- 指标 DAG 展开 ----------------
    def _alias_for(self, measure_name: str) -> str:
        if measure_name not in self._measure_alias:
            self._measure_alias[measure_name] = f"m_{len(self._measure_alias)}"
            self._leaves[measure_name] = self.reg.measures[measure_name]
        return self._measure_alias[measure_name]

    def _resolve(self, name: str, stack: list) -> str:
        if name in stack:
            raise PlanError("metric_cycle", chain=stack + [name])
        if name in self._resolved:
            return self._resolved[name]
        if name in self.reg.unavailable:
            raise PlanError("unavailable_metric", name=name,
                            reason=self.reg.unavailable[name]["reason"])
        if name in self.reg.ratios:
            expr = self._subst(self.reg.ratios[name]["expr"], stack + [name])
        elif name in self.reg.metrics:
            expr = self._alias_for(self.reg.metrics[name]["measure"])
        else:
            raise PlanError("unknown_metric", name=name,
                            available=self.reg.all_metric_names())
        self._resolved[name] = expr
        return expr

    def _subst(self, expr: str, stack: list) -> str:
        for n in sorted(self.reg.all_metric_names(), key=len, reverse=True):
            if n in expr:
                expr = expr.replace(n, "(" + self._resolve(n, stack) + ")")
        return expr

    # ---------------- 主流程 ----------------
    def plan(self, spec: dict, tenant=1) -> Plan:
        if isinstance(tenant, bool) or not isinstance(tenant, int):
            raise PlanError("invalid_tenant", reason="tenant 必须是整数")
        # Planner 可被验收/服务复用；每次计划都从干净的指标 DAG 开始。
        self._measure_alias = {}
        self._leaves = {}
        self._resolved = {}
        metrics = spec.get("metrics") or []
        if not metrics:
            raise PlanError("invalid_spec", reason="metrics 不能为空")
        dims = spec.get("dimensions") or []
        grain = spec.get("time_grain", "total")
        if grain not in ("total", "day"):
            raise PlanError("invalid_spec", reason=f"暂不支持 time_grain={grain}")

        compare = spec.get("compare")
        if compare is not None:
            if not isinstance(compare, dict) or not compare.get("start") or not compare.get("end"):
                raise PlanError("invalid_spec", reason="compare 必须包含 start/end")
            # 两期日期窗无法与按天的行一一对齐；按天对比留给调用方分别查询。
            if grain == "day":
                raise PlanError("compare_with_grain",
                                reason="compare 与 time_grain=day 不能同时使用")

        for m in metrics:
            self._resolve(m, [])

        models = {leaf["model"] for leaf in self._leaves.values()}
        for d in dims:
            for mo in models:
                if self.reg.is_hidden(mo, d):
                    raise PlanError("column_hidden", column=d, model=mo,
                                    reason=f"模型 {mo} 的列 {d} 已标记为 hidden")
                if d not in self.reg.models[mo].get("dimensions", []):
                    raise PlanError("dimension_unavailable", dimension=d, model=mo)
        for f in spec.get("filters") or []:
            dimension = f.get("dimension")
            for mo in models:
                if self.reg.is_hidden(mo, dimension):
                    raise PlanError("column_hidden", column=dimension, model=mo,
                                    reason=f"模型 {mo} 的列 {dimension} 已标记为 hidden")

        output_dims = list(dims) + (["day"] if grain == "day" else [])
        # compare 时两期要落在同一行；没有 compare 时保留原有按天对齐行为。
        align_on = list(dims) if compare is not None else list(output_dims)

        by_model = {}
        for ms, mdef in self._leaves.items():
            by_model.setdefault(mdef["model"], []).append((ms, mdef))

        # 每个叶子度量都先生成本期 alias(m_0)，对比期使用同一 alias 的
        # _prev 后缀。这样比率/派生式只需替换叶子别名，不会重复解析指标 DAG。
        periods = [("", spec.get("time_range") or {})]
        if compare is not None:
            periods.append(("_prev", compare))

        nodes = []
        for suffix, time_range in periods:
            for mo, items in by_model.items():
                m = self.reg.models[mo]
                if grain == "day" and "pay_time" not in m.get("dimensions", []) \
                        and "date" not in m.get("dimensions", []):
                    raise PlanError("time_grain_unavailable", model=mo, time_grain=grain,
                                    reason=f"模型 {mo} 没有时间维度, 无法按 {grain} 拆")
                if m["provider"] == "platform_mcp":
                    for ms, mdef in items:
                        tool = m.get("mcp_tool")
                        if not tool:
                            raise PlanError("provider_config", model=mo,
                                            reason="platform_mcp 模型缺 mcp_tool 声明")
                        # 时间窗: 平台侧时间列固定叫 date; 透传 start/end, 分组 Python 侧做
                        args = {"start": time_range.get("start"),
                                "end": time_range.get("end")}
                        for f in spec.get("filters") or []:
                            if f["dimension"] in m.get("dimensions", []):
                                args[f["dimension"]] = f.get("value")
                        nodes.append(AggNode(
                            provider="platform_mcp", model=mo, sql="",
                            output_keys=(["day"] if grain == "day" else [])
                            + [d for d in dims if d in m.get("dimensions", [])] + [mdef["column"]],
                            mcp_call={"tool": tool, "args": args},
                            value_column=mdef["column"],
                            measure_alias=self._measure_alias[ms] + suffix))
                    continue
                if m["provider"] != "sqlite":
                    raise PlanError("provider_unsupported", provider=m["provider"], model=mo)
                select, group = [], []
                if grain == "day":
                    select.append("DATE(pay_time) AS day")
                    group.append("DATE(pay_time)")
                for d in dims:
                    select.append(d)
                    group.append(d)
                for ms, mdef in items:
                    alias = self._measure_alias[ms] + suffix
                    select.append(f"{self._agg_sql(mdef)} AS {alias}")
                where = self._where(time_range, spec.get("filters") or [],
                                    scope=m.get("scope"), tenant=tenant)
                sql = f"SELECT {', '.join(select)} FROM ({m['ref_sql']}) AS {mo}"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                if group:
                    sql += " GROUP BY " + ", ".join(group)
                nodes.append(AggNode(
                    provider="sqlite", model=mo, sql=sql,
                    output_keys=align_on + [self._measure_alias[ms] + suffix
                                            for ms, _ in items]))

        derived = []
        for name in metrics:
            meta = self.reg.ratios.get(name) or self.reg.metrics.get(name) or {}
            curr_expr = self._resolve(name, [])
            if compare is None:
                derived.append({"name": name, "expr": curr_expr,
                                "unit": meta.get("unit", "")})
                continue
            prev_expr = self._prev_expr(curr_expr)
            derived.extend([
                {"name": name, "expr": curr_expr, "unit": meta.get("unit", "")},
                {"name": f"{name}_prev", "expr": prev_expr, "unit": meta.get("unit", "")},
                {"name": f"{name}_delta", "expr": f"({curr_expr}) - ({prev_expr})",
                 "unit": meta.get("unit", "")},
                {"name": f"{name}_pct",
                 "expr": f"(({curr_expr}) - ({prev_expr})) / ({prev_expr}) * 100",
                 "unit": "%"},
            ])

        return Plan(spec=spec, nodes=nodes, align_on=align_on, derived=derived,
                    output_dims=output_dims, order_by=spec.get("order_by") or [],
                    limit=spec.get("limit"))

    def _prev_expr(self, expr: str) -> str:
        """把已解析的本期叶子 alias 精确替换为对比期 alias。"""
        for alias in sorted(self._measure_alias.values(), key=len, reverse=True):
            expr = re.sub(rf"\b{re.escape(alias)}\b", alias + "_prev", expr)
        return expr

    # ---------------- SQL 片段 ----------------
    @staticmethod
    def _agg_sql(mdef: dict) -> str:
        if mdef["agg"] == "count":
            return f"COUNT(DISTINCT {mdef['distinct']})" if mdef.get("distinct") else "COUNT(*)"
        return f"{AGG_FN[mdef['agg']]}({mdef['column']})"

    @staticmethod
    def _where(tr: dict, filters: list, scope: dict = None, tenant=1) -> list:
        conds = []
        if scope:
            conds.append(f"{scope['column']} = {tenant}")
        if tr.get("start"):
            conds.append(f"pay_time >= '{tr['start']}'")
        if tr.get("end"):
            conds.append(f"pay_time < '{tr['end']}'")
        for f in filters:
            d, op, v = f["dimension"], f.get("op", "="), f.get("value")
            if op == "=" and isinstance(v, str):
                conds.append(f"{d} = '{v}' COLLATE NOCASE")
            else:
                conds.append(f"{d} {op} '{v}'")
        return conds
