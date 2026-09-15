"""确定性业务公式。

本模块只做数学计算：不读取配置、不访问数据库，也不依赖 agent runtime。
"""

import math


_RECONCILIATION_WARNING = "重建总量与实测偏差超过容差, 口径可能不一致"


def _is_finite_number(value) -> bool:
    """判断值是否可以安全地参与对数计算。"""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _undefined_result(total_curr, total_prev, reason: str) -> dict:
    try:
        total_delta = total_curr - total_prev
        if not _is_finite_number(total_delta):
            total_delta = None
    except (TypeError, ValueError, OverflowError):
        total_delta = None
    return {
        "status": "undefined",
        "reason": reason,
        "total_delta": total_delta,
    }


def lmdi(total_curr, total_prev, factors: dict, tolerance: float = 0.02) -> dict:
    """乘法关系的 LMDI 加法分解。

    ``factors`` 的形状为 ``{"因素名": {"curr": x1, "prev": x0}}``。
    总量和所有可参与分解的因素必须为正数；单个因素为 0（或其它
    非正/无效值）时只跳过该因素，并把未解释部分作为 ``residual``
    暴露出来，而不是把它伪装成 0。

    这是纯函数：对账只使用调用者传入的总量和因素值，容差也由参数
    传入，默认 2%。
    """
    if not _is_finite_number(total_curr) or not _is_finite_number(total_prev):
        return _undefined_result(total_curr, total_prev, "总量不是有限数, 乘法分解不适用")

    total_delta = total_curr - total_prev
    if total_curr == 0 or total_prev == 0:
        return _undefined_result(total_curr, total_prev, "总量含0, 乘法分解不适用")
    if total_curr < 0 or total_prev < 0:
        return _undefined_result(total_curr, total_prev, "总量含负数, 乘法分解不适用")
    if not isinstance(factors, dict):
        raise TypeError("factors 必须是 dict")

    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("tolerance 必须是数字") from exc
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance 必须是非负有限数")

    # 对数均值权。总量相等时取连续极限 w=X，而不是计算 0/0。
    log_total_ratio = math.log(float(total_curr) / float(total_prev))
    weight = float(total_curr) if log_total_ratio == 0 else total_delta / log_total_ratio

    contributions = {}
    unavailable = []
    current_values = []
    for name, pair in factors.items():
        if not isinstance(pair, dict) or "curr" not in pair or "prev" not in pair:
            contributions[name] = None
            unavailable.append(name)
            continue

        curr, prev = pair["curr"], pair["prev"]
        if not _is_finite_number(curr) or not _is_finite_number(prev):
            contributions[name] = None
            unavailable.append(name)
            continue

        current_values.append(curr)
        # log(curr / prev) 只在两期都为正时有定义。0 因素按 D3
        # 显式记为 None；负因素也不伪造贡献。
        if curr <= 0 or prev <= 0:
            contributions[name] = None
            unavailable.append(name)
        else:
            contributions[name] = weight * math.log(float(curr) / float(prev))

    valid_sum = sum(v for v in contributions.values() if v is not None)
    closure = valid_sum - total_delta
    has_unavailable = bool(unavailable)
    residual = total_delta - valid_sum if has_unavailable else 0.0

    if total_delta == 0:
        shares = {name: None for name in contributions}
    else:
        shares = {
            name: (None if value is None else value / total_delta * 100)
            for name, value in contributions.items()
        }

    warnings = []
    if has_unavailable and abs(residual) > abs(total_delta) * 0.5:
        names = ", ".join(str(name) for name in unavailable)
        warnings.append(
            f"因素 {names} 不适用, 残差 {residual} 超过总变化绝对值的50%, 分解不完整"
        )

    # 重建校验使用本期因素的乘积。正常输入下 current_values 与因素数
    # 相同；若存在缺失结构则无法可靠重建，仍返回结构化结果而不编数字。
    if len(current_values) == len(factors):
        reconstructed = math.prod(current_values)
        rel_error = abs(reconstructed - total_curr) / abs(total_curr)
        reconciliation = {
            "reconstructed": reconstructed,
            "actual": total_curr,
            "rel_error": rel_error,
            "tolerance": tolerance,
        }
        if rel_error > tolerance:
            reconciliation["ok"] = False
            reconciliation["warning"] = _RECONCILIATION_WARNING
            warnings.append(_RECONCILIATION_WARNING)
        else:
            reconciliation["ok"] = True
    else:
        reconciliation = {
            "reconstructed": None,
            "actual": total_curr,
            "rel_error": None,
            "tolerance": tolerance,
            "ok": False,
            "warning": "因素值缺失, 无法重建总量",
        }
        warnings.append(reconciliation["warning"])

    return {
        "status": "ok",
        "total_delta": total_delta,
        "weight": weight,
        "w": weight,
        "contributions": contributions,
        "shares": shares,
        "closure": closure,
        "residual": residual,
        "warnings": warnings,
        "reconciliation": reconciliation,
    }
