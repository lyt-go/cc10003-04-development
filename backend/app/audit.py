"""查询影响记录：除运营概览之外，聚合查询下钻链路的可核对操作流水。

每条记录固定带上模板编号、模板版本、下钻路径、层 级、动作、结果，
供配置页和接口按模板版本核对「谁在什么时候启用/展开了哪一层」。
内部表不进入运营概览，避免把查询流水算进业务模块统计。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "_query_audit"


def append(
    *,
    action: str,
    result: str,
    template_code: str | None = None,
    version: int | None = None,
    path_id: int | None = None,
    path_code: str | None = None,
    level: int | None = None,
    operator: str | None = None,
    detail: str = "",
    **extra: Any,
) -> dict[str, Any]:
    rows = store.rows(MODULE)
    entry: dict[str, Any] = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "时间": _now(),
        "动作": action,
        "结果": result,
        "模板编号": template_code,
        "模板版本": version,
        "路径ID": path_id,
        "路径编号": path_code,
        "层级": level,
        "操作人": operator,
        "说明": detail,
    }
    entry.update({key: value for key, value in extra.items() if value is not None})
    rows.append(entry)
    return entry


def rows() -> list[dict[str, Any]]:
    return store.rows(MODULE)


def list_entries(
    *,
    template_code: str | None = None,
    version: int | None = None,
    path_id: int | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    result = list(reversed(rows()))
    if template_code:
        result = [row for row in result if str(row.get("模板编号")) == template_code]
    if version is not None:
        result = [row for row in result if row.get("模板版本") == version]
    if path_id is not None:
        result = [row for row in result if row.get("路径ID") == path_id]
    if action:
        result = [row for row in result if row.get("动作") == action]
    total = len(result)
    return result[offset:offset + limit], total


def _now() -> str:
    from app.snapshots import utc_now
    return utc_now()
