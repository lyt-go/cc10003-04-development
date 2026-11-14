"""启动时对齐示例数据：已启用的模板补齐 v1 快照，已启用的路径补齐同版本绑定。

真实项目里这些状态由历史操作产生；示例数据是静态的，所以这里只在
「模板已启用但还没有任何版本快照」时补一份，避免示例数据自相矛盾。
"""
from __future__ import annotations

from app.snapshots import (
    TEMPLATE_VERSION_KEY,
    build_cache_entries,
    build_snapshot,
    coerce_int,
    current_version,
    next_version,
    split_fields,
    store_snapshot,
)
from app.store import store

_DONE = False


def ensure_seed_state() -> None:
    global _DONE
    if _DONE:
        return
    _DONE = True
    _bootstrap_templates()
    _bootstrap_paths()


def _bootstrap_templates() -> None:
    for entry in store.rows("queryboard"):
        if str(entry.get("status")) != "已启用":
            continue
        template_code = str(entry.get("模板编号"))
        if current_version(template_code) is not None:
            continue
        dimensions = split_fields(entry.get("维度字段"))
        measures = split_fields(entry.get("度量字段"))
        if not dimensions or not measures:
            continue
        version = next_version(template_code)
        snapshot = build_snapshot(
            template_code=template_code,
            version=version,
            dimensions=dimensions,
            measures=measures,
            data_source=str(entry.get("数据源") or ""),
        )
        build_cache_entries(snapshot, status="当前")
        store_snapshot(snapshot, operator="系统迁移", source_status="已启用")
        entry["模板版本"] = version
        entry["版本标识"] = snapshot["版本标识"]
        entry["允许的下钻层级"] = snapshot["允许的下钻层级"]
        entry["快照摘要"] = snapshot["摘要校验值"]
        entry["快照生成时间"] = snapshot.get("生成时间")


def _bootstrap_paths() -> None:
    from app.services.datadrill import DatadrillService

    service = DatadrillService()
    for entry in store.rows("datadrill"):
        if str(entry.get("status")) != "已启用":
            continue
        if service.current_binding(int(entry["id"])) is not None:
            continue
        template_code = str(entry.get("关联模板") or "")
        current = current_version(template_code)
        if current is None:
            continue
        snapshot = current.get("快照") or {}
        dimensions = list(snapshot.get("维度字段") or [])
        declared = split_fields(entry.get("每层维度名"))
        if declared != dimensions:
            entry["每层维度名"] = ",".join(dimensions)
            entry["下钻层级"] = ",".join(dimensions)
            entry["每层度量"] = ",".join(
                str(level.get("度量字段")) for level in snapshot.get("层级摘要") or []
            )
            entry["路径深度"] = len(dimensions)
        entry["关联模板版本"] = coerce_int(current.get(TEMPLATE_VERSION_KEY))
        entry["快照摘要"] = snapshot.get("摘要校验值")
        service.bind_existing(entry, current, operator="系统迁移")
