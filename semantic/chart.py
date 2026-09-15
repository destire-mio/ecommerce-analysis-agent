"""把 query_metrics 行转换成前端可直接渲染的 ECharts option。"""


_CHART_TYPES = {"bar", "line", "pie"}


def _value(columns, row, name):
    if isinstance(row, dict):
        return row.get(name)
    return row[columns.index(name)]


def to_echarts(columns, rows, chart_type: str, x: str, y: str) -> dict:
    """纯函数：不查询数据库、不落盘，只生成最小 ECharts 配置。"""
    if chart_type not in _CHART_TYPES:
        raise ValueError("unknown_chart_type")
    columns = list(columns or [])
    if x not in columns:
        raise ValueError(f"unknown_x:{x}")
    if y not in columns:
        raise ValueError(f"unknown_y:{y}")
    rows = list(rows or [])
    x_data = [_value(columns, row, x) for row in rows]
    y_data = [_value(columns, row, y) for row in rows]
    if chart_type == "pie":
        return {
            "tooltip": {"trigger": "item"},
            "series": [{
                "type": "pie",
                "data": [{"name": xv, "value": yv} for xv, yv in zip(x_data, y_data)],
            }],
        }
    return {
        "xAxis": {"type": "category", "data": x_data},
        "yAxis": {"type": "value"},
        "series": [{"type": chart_type, "data": y_data}],
    }


def chart_types() -> tuple:
    """给工具 schema/测试使用的稳定白名单。"""
    return tuple(sorted(_CHART_TYPES))
