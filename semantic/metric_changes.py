"""受控指标口径变更：agent 只写草稿，人审批后才改 mdl.yaml。"""

from copy import deepcopy
from datetime import date, datetime
import json
import os
import re
import secrets

import yaml

from .registry import Registry, RegistryError, load as load_registry


class MetricChangeError(ValueError):
    """草稿不存在、字段非法或基于过期口径时抛出。"""


# 版本/日志/生效时间由审批流程维护，不能被草稿直接改写。
_MUTABLE_FIELDS = {"description", "unit", "measure", "expr", "model", "owner"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _root(root) -> str:
    return os.fspath(root)


def _mdl_path(metrics_root: str) -> str:
    return os.path.join(_root(metrics_root), "mdl.yaml")


def _changes_dir(metrics_root: str) -> str:
    return os.path.join(_root(metrics_root), "metric_changes")


def _load_data(metrics_root: str) -> dict:
    path = _mdl_path(metrics_root)
    if not os.path.exists(path):
        raise MetricChangeError(f"找不到语义层文件: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise MetricChangeError("mdl.yaml 顶层必须是对象")
    return data


def _locate(data: dict, metric: str):
    """返回 (section, definition)，section 是 metrics 或 ratios。"""
    for section in ("metrics", "ratios"):
        for definition in data.get(section, []) or []:
            if definition.get("name") == metric:
                return section, definition
    raise MetricChangeError(f"未知指标: {metric}")


def _validate_field(definition: dict, field: str):
    if field not in _MUTABLE_FIELDS or field not in definition:
        allowed = sorted(_MUTABLE_FIELDS & set(definition))
        raise MetricChangeError(
            f"字段不可提议: {field}; {definition.get('name')} 可改字段: {allowed}")


def _draft_path(metrics_root: str, change_id: str) -> str:
    if not isinstance(change_id, str) or not _ID_RE.fullmatch(change_id):
        raise MetricChangeError(f"非法变更 id: {change_id}")
    return os.path.join(_changes_dir(metrics_root), change_id + ".json")


def _write_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def propose(metrics_root, metric: str, field: str, new_value, reason: str,
            proposed_by: str) -> dict:
    """写一条 pending 草稿；绝不修改 mdl.yaml。"""
    root = _root(metrics_root)
    data = _load_data(root)
    _section, definition = _locate(data, metric)
    _validate_field(definition, field)
    if not isinstance(reason, str) or not reason.strip():
        raise MetricChangeError("reason 不能为空")
    if not isinstance(proposed_by, str) or not proposed_by.strip():
        raise MetricChangeError("proposed_by 不能为空")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while True:
        change_id = f"mc-{stamp}-{secrets.token_hex(2)}"
        path = _draft_path(root, change_id)
        if not os.path.exists(path):
            break
    draft = {
        "id": change_id,
        "metric": metric,
        "field": field,
        "old": deepcopy(definition[field]),
        "new": new_value,
        "reason": reason.strip(),
        "proposed_by": proposed_by,
        "proposed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }
    _write_json(path, draft)
    return draft


def list_pending(metrics_root) -> list:
    """列出草稿区中状态为 pending 的变更，按提出时间/ID稳定排序。"""
    directory = _changes_dir(_root(metrics_root))
    if not os.path.isdir(directory):
        return []
    out = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                draft = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if draft.get("status") == "pending":
            out.append(draft)
    out.sort(key=lambda d: (d.get("proposed_at", ""), d.get("id", "")))
    return out


def _read_draft(metrics_root: str, change_id: str) -> tuple:
    path = _draft_path(metrics_root, change_id)
    if not os.path.exists(path):
        raise MetricChangeError(f"找不到变更草稿: {change_id}")
    try:
        with open(path, encoding="utf-8") as fh:
            draft = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise MetricChangeError(f"草稿无法读取: {change_id}") from exc
    if draft.get("status") != "pending":
        raise MetricChangeError(f"变更 {change_id} 当前状态不是 pending: {draft.get('status')}")
    return path, draft


def approve(metrics_root, change_id: str, reject: bool = False,
            approved_by: str = None, db_path: str = None) -> dict:
    """审批/驳回草稿；批准前强制检查 old 与当前 mdl.yaml 一致。"""
    root = _root(metrics_root)
    draft_path, draft = _read_draft(root, change_id)
    if reject:
        draft["status"] = "rejected"
        draft["rejected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _write_json(draft_path, draft)
        return {"status": "rejected", "id": change_id, "path": draft_path}

    data = _load_data(root)
    section, definition = _locate(data, draft.get("metric"))
    field = draft.get("field")
    _validate_field(definition, field)
    if definition[field] != draft.get("old"):
        raise MetricChangeError(
            f"变更 {change_id} 基于过期值: {draft['metric']}.{field} 当前值与草稿 old 不一致")

    current_version = definition.get("version")
    if isinstance(current_version, bool) or not isinstance(current_version, int) or current_version < 1:
        raise MetricChangeError(f"{draft['metric']} 当前 version 非法: {current_version}")
    log = definition.get("change_log")
    if not isinstance(log, list) or not log:
        raise MetricChangeError(f"{draft['metric']} 当前 change_log 非法")

    candidate = deepcopy(data)
    _section, candidate_definition = _locate(candidate, draft["metric"])
    candidate_definition[field] = draft.get("new")
    new_version = current_version + 1
    effective = date.today().isoformat()
    candidate_definition["version"] = new_version
    candidate_definition["effective_date"] = effective
    candidate_definition.setdefault("change_log", []).append({
        "version": new_version,
        "date": effective,
        "by": approved_by or draft.get("proposed_by") or "human",
        "note": draft.get("reason", ""),
    })

    # 先在内存候选上做和启动期相同的结构校验，失败时不碰权威文件。
    try:
        Registry(candidate).validate(db_path=db_path)
    except RegistryError as exc:
        raise MetricChangeError(f"批准后的 mdl.yaml 校验失败: {exc}") from exc

    mdl_path = _mdl_path(root)
    with open(mdl_path, encoding="utf-8") as fh:
        original_mdl = fh.read()
    tmp = mdl_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(candidate, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, mdl_path)
    try:
        # 从实际写入的文件重新加载一次，确保审批产物与启动期加载路径一致。
        load_registry(mdl_path, db_path=db_path)
    except RegistryError as exc:
        with open(mdl_path, "w", encoding="utf-8") as fh:
            fh.write(original_mdl)
        raise MetricChangeError(f"批准后的 mdl.yaml 重载失败: {exc}") from exc

    draft["status"] = "approved"
    draft["approved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    if approved_by:
        draft["approved_by"] = approved_by
    _write_json(draft_path, draft)
    return {"status": "approved", "id": change_id, "metric": draft["metric"],
            "version": new_version, "effective_date": effective,
            "path": draft_path, "section": section}
