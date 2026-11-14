"""聚合查询接口：维护查询模板，覆盖提交模板验证、启用模板、废弃模板等动作。

模板级下钻快照在「提交模板验证」时固化；下钻路径启用后只读取同版本模板。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app import audit
from app.schemas import ActionResult, EntryPayload, PageResult
from app.security import Actor, get_actor
from app.services.queryboard import QueryboardService

router = APIRouter(prefix="/api/queryboard", tags=["聚合查询"])

service = QueryboardService()

LIST_FIELDS = ["模板编号", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"]
STATUSES = ["草稿", "模板验证中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号或指标名称检索"),
    status: str | None = Query(default=None, description="草稿、模板验证中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤聚合查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 静态路径必须注册在 /{entry_id} 之前，否则 "export"/"audit" 会被当成 id 解析。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "queryboard", "total": total, "items": items}


@router.get("/audit")
def query_audit(
    template_code: str | None = Query(default=None, alias="模板编号"),
    version: int | None = Query(default=None, alias="模板版本"),
    path_id: int | None = Query(default=None, alias="路径ID"),
    action: str | None = Query(default=None, alias="动作"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """查询影响记录：按模板编号/版本核对启用、展开与拒绝流水。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = audit.list_entries(
        template_code=template_code,
        version=version,
        path_id=path_id,
        action=action,
        limit=size,
        offset=max(page - 1, 0) * size,
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions", response_model=dict)
def list_versions(entry_id: int) -> dict:
    """查看模板的快照版本历史；旧版本保留供下钻路径核对历史结果。"""
    entry, versions = service.versions(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return {"模板编号": entry.get("模板编号"), "items": versions, "total": len(versions)}


@router.get("/{entry_id}/snapshot", response_model=dict)
def get_snapshot(
    entry_id: int,
    version: int | None = Query(default=None, description="不传时读取当前版本"),
    include_results: bool = Query(default=True, description="是否返回每层聚合结果"),
) -> dict:
    """读取模板级下钻快照：维度字段、度量字段、允许层级与聚合摘要。"""
    entry, snapshot, error = service.snapshot_for(
        entry_id, version=version, include_results=include_results
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=error or f"查询模板 {entry_id} 不存在")
    if snapshot is None:
        raise HTTPException(status_code=409, detail=error or "模板快照不可用")
    return snapshot


@router.get("/{entry_id}/cache", response_model=dict)
def get_cache(entry_id: int) -> dict:
    """查看模板各版本的下钻缓存摘要，当前/历史状态一目了然。"""
    entry, items = service.cache_summary(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return {"模板编号": entry.get("模板编号"), "items": items, "total": len(items)}


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    actor: Actor = Depends(get_actor),
) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values, actor=actor)
    if entry is None:
        if isinstance(missing, str):
            return ActionResult(ok=False, message=missing)
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    actor: Actor = Depends(get_actor),
) -> ActionResult:
    """对单条查询模板执行提交模板验证、启用模板、废弃模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.action or payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, actor=actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
