"""语义登记表: 加载 + 加载期校验。

读 data/ecommerce/mdl.yaml, 编译成内存 Registry。
机器管确定性: 所有结构性错误在加载期挡在门外
(列名对齐、度量/指标指向、单位齐全、声明主键在数据中真实唯一)。
"""

import os
import sqlite3

import yaml
from sqlglot import parse_one


class RegistryError(Exception):
    pass


def _select_output_columns(sql: str) -> list:
    tree = parse_one(sql, dialect="sqlite")
    return [s.alias_or_name for s in tree.expressions]


class Registry:
    def __init__(self, data: dict):
        self.raw = data
        self.models = {m["name"]: m for m in data.get("models", [])}
        self.measures = {m["name"]: m for m in data.get("measures", [])}
        self.metrics = {m["name"]: m for m in data.get("metrics", [])}
        self.ratios = {m["name"]: m for m in data.get("ratios", [])}
        self.unavailable = {m["name"]: m for m in data.get("unavailable", [])}
        self.identities = {r["name"]: r for r in data.get("identities", [])}
        self.reconciliations = {r["name"]: r for r in data.get("reconciliations", [])}

    # ---------------- 加载期校验 ----------------
    def validate(self, db_path: str = None):
        for name, m in self.models.items():
            for field in ("description", "grain", "key", "provider", "columns"):
                if field not in m:
                    raise RegistryError(f"model '{name}' 缺少字段 {field}")
            if m["provider"] not in ("sqlite", "platform_mcp"):
                raise RegistryError(f"model '{name}' 未知 provider '{m['provider']}'")
            if m["provider"] == "platform_mcp" and not m.get("mcp_tool"):
                raise RegistryError(f"model '{name}' platform_mcp 必须声明 mcp_tool")
            cols = [c["name"] for c in m["columns"]]
            for c in m["columns"]:
                if "unit" not in c:
                    raise RegistryError(f"model '{name}' 列 '{c['name']}' 缺 unit")
            if "ref_sql" in m and "table" not in m:
                out = _select_output_columns(m["ref_sql"])
                if out != cols:
                    raise RegistryError(
                        f"model '{name}': ref_sql 输出列 {out} != 声明列 {cols}"
                        " (第 0 层要求严格相等)")
            for d in m.get("dimensions", []):
                if d not in cols:
                    raise RegistryError(f"model '{name}' 维度 '{d}' 不在列里")
            for k in m["key"]:
                if k not in cols:
                    raise RegistryError(f"model '{name}' 主键 '{k}' 不在列里")

        if db_path:
            self._validate_key_uniqueness(db_path)

        for name, s in self.measures.items():
            if s["model"] not in self.models:
                raise RegistryError(f"measure '{name}' 指向未知模型 '{s['model']}'")
            for field in ("agg", "unit", "additivity", "description"):
                if field not in s:
                    raise RegistryError(f"measure '{name}' 缺少字段 {field}")
            if s["agg"] not in ("sum", "count", "avg", "max", "min"):
                raise RegistryError(f"measure '{name}' 未知聚合 '{s['agg']}'")
            if s["agg"] != "count" and "column" not in s:
                raise RegistryError(f"measure '{name}' 缺少 column")

        for name, mt in self.metrics.items():
            if "measure" not in mt:
                raise RegistryError(f"metric '{name}' 缺少 measure")
            if "description" not in mt:
                raise RegistryError(f"metric '{name}' 缺少 description")
            if "unit" not in mt:
                raise RegistryError(f"metric '{name}' 缺少 unit")
            if mt["measure"] not in self.measures:
                raise RegistryError(f"metric '{name}' 指向未知 measure '{mt['measure']}'")

        for name, r in self.ratios.items():
            if not r.get("expr") or "unit" not in r or "description" not in r:
                raise RegistryError(f"ratio '{name}' 缺少 expr/unit/description")

        self._validate_rules("identity", self.identities, "derived_identity")
        self._validate_rules("reconciliation", self.reconciliations, "reconciliation_rule")

    def _validate_rules(self, section: str, rules: dict, expected_type: str):
        """校验恒等/对账声明的结构；表达式解释仍留给后续能力。"""
        registered = set(self.all_metric_names())
        for name, rule in rules.items():
            for field in ("total", "factors", "type", "tolerance"):
                if field not in rule:
                    raise RegistryError(f"{section} '{name}' 缺少字段 {field}")
            if rule["type"] != expected_type:
                raise RegistryError(
                    f"{section} '{name}' type 应为 '{expected_type}', 收到 '{rule['type']}'")
            if not isinstance(rule["factors"], list) or not rule["factors"]:
                raise RegistryError(f"{section} '{name}' factors 必须是非空列表")
            if rule["total"] not in registered:
                raise RegistryError(
                    f"{section} '{name}' total '{rule['total']}' 不是已登记指标名")
            unknown = [f for f in rule["factors"] if f not in registered]
            if unknown:
                raise RegistryError(
                    f"{section} '{name}' factors 含未登记指标: {unknown}")
            if isinstance(rule["tolerance"], bool) or not isinstance(
                    rule["tolerance"], (int, float)) or rule["tolerance"] < 0:
                raise RegistryError(f"{section} '{name}' tolerance 必须是非负数字")

    def _validate_key_uniqueness(self, db_path: str):
        """声明的主键必须真实唯一: 查库验证, 有重复行 = 假主键 = 拒绝加载."""
        conn = sqlite3.connect(db_path)
        try:
            for name, m in self.models.items():
                if "ref_sql" not in m or "table" in m:
                    continue  # 物理表型模型的唯一性由表约束保证
                key = m["key"]
                src = f"({m['ref_sql']})"
                kcols = ", ".join(key)
                sql = (f"SELECT {kcols}, COUNT(*) AS n FROM {src} "
                       f"GROUP BY {kcols} HAVING COUNT(*) > 1 LIMIT 1")
                dup = conn.execute(sql).fetchone()
                if dup:
                    raise RegistryError(
                        f"model '{name}': 声明主键 {key} 在数据中不唯一 "
                        f"(重复示例: {dict(zip(key, dup[:-1]))}, 出现 {dup[-1]} 次)。"
                        "请修正 key 或修数据——粒度声明不能是空话。")
        finally:
            conn.close()

    # ---------------- 查询辅助 ----------------
    def all_metric_names(self) -> list:
        return list(self.metrics) + list(self.ratios)

    def lookup_metric(self, name: str):
        return self.metrics.get(name) or self.ratios.get(name)


def load(path: str, db_path: str = None) -> Registry:
    if not os.path.exists(path):
        raise RegistryError(f"找不到语义层文件: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    reg = Registry(data)
    reg.validate(db_path=db_path)
    return reg


def describe(reg: Registry) -> str:
    """生成给 agent 的语义索引(材料包用)——不手写第二份, 防漂移。"""
    lines = ["可用语义模型:"]
    for name, m in reg.models.items():
        cols = ", ".join(c["name"] for c in m["columns"])
        lines.append(f"- {name}(粒度 {m['grain']}): {m.get('description', '')} [列: {cols}]")
    lines.append("可用指标:")
    for name, mt in reg.metrics.items():
        lines.append(f"- {name}({mt.get('unit', '')}): {mt.get('description', '')}")
    for name, r in reg.ratios.items():
        lines.append(f"- {name}({r.get('unit', '')}): {r.get('description', '')}")
    if reg.unavailable:
        lines.append("未开放指标(禁止给数字): " + "、".join(reg.unavailable))
    if reg.identities:
        lines.append("已声明恒等式: " + "、".join(
            f"{name}({r.get('total')} = {'×'.join(r.get('factors') or [])})"
            for name, r in reg.identities.items()))
    if reg.reconciliations:
        lines.append("已声明对账规则: " + "、".join(
            f"{name}(容差 {r.get('tolerance')})"
            for name, r in reg.reconciliations.items()))
    return "\n".join(lines)
