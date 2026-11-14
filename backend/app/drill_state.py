"""下钻路径与模板版本的绑定状态（内部表）。

聚合查询与多维下钻两个服务都要操作这张表：
- 模板换版时把旧版本绑定转历史，旧路径保留历史层级结果；
- 路径重新启用时生成同版本绑定并让旧绑定转历史；
- 两个服务共用同一把模板锁，保证「启用模板」和「启用路径」不会交错。
"""
from __future__ import annotations

import threading
from typing import Any

from app.snapshots import TEMPLATE_CODE_KEY, TEMPLATE_VERSION_KEY, coerce_int, utc_now

BINDING_MODULE = "_drill_bindings"

_TEMPLATE_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def template_lock(template_code: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _TEMPLATE_LOCKS.setdefault(template_code, threading.RLock())


def binding_rows() -> list[dict[str, Any]]:
    from app.store import store
    return store.rows(BINDING_MODULE)


def mark_bindings_historical(path_id: int) -> int:
    changed = 0
    for row in binding_rows():
        if coerce_int(row.get("路径ID")) == path_id and row.get("状态") == "当前":
            row["状态"] = "历史"
            changed += 1
    return changed


def invalidate_bindings_for_template(template_code: str, current_version: int) -> int:
    """模板换版时：非当前版本的路径绑定转历史，旧路径保留历史层级结果。

    绑定转历史后 ``可展开`` 变为 False，新的展开请求会明确提示模板已换版，
    不会拿旧版本的缓存去拼新版本的结果。
    """
    changed = 0
    for row in binding_rows():
        if str(row.get(TEMPLATE_CODE_KEY)) != template_code:
            continue
        if coerce_int(row.get(TEMPLATE_VERSION_KEY)) == current_version:
            continue
        if row.get("状态") == "当前":
            row["状态"] = "历史"
            changed += 1
    return changed


def create_binding(
    *,
    path_id: int,
    path_code: str | None,
    template_code: str,
    version: int,
    snapshot_digest: str | None,
    levels: list[dict[str, Any]],
    operator: str,
) -> dict[str, Any]:
    mark_bindings_historical(path_id)
    rows = binding_rows()
    binding = {
        "id": max((coerce_int(row.get("id")) for row in rows), default=0) + 1,
        "路径ID": path_id,
        "路径编号": path_code,
        TEMPLATE_CODE_KEY: template_code,
        TEMPLATE_VERSION_KEY: version,
        "快照摘要": snapshot_digest,
        "层级结构": levels,
        "状态": "当前",
        "启用人": operator,
        "启用时间": utc_now(),
    }
    rows.append(binding)
    return binding
