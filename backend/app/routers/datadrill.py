"""多维下钻接口：维护下钻路径，覆盖提交路径评审、启用路径、关闭路径等动作。

启用后的路径按层级展开，只读取同一模板版本的快照与缓存。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.security import Actor, get_actor
from app.services.datadrill import DatadrillService

router = APIRouter(prefix="/api/datadrill", tags=["多维下钻"])

service = DatadrillService()

LIST_FIELDS = ["路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量", "路径深度", "默认展开层", "关联看板"]
STATUSES = ["草稿", "路径评审中", "已启用", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号或关联模板检索"),
    status: str | None = Query(default=None, description="草稿、路径评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datadrill", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/bindings", response_model=dict)
def list_bindings(entry_id: int) -> dict:
    """查看路径的模板版本绑定历史：旧版本的层级结果保留供核对。"""
    entry, items = service.bindings(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return {"路径编号": entry.get("路径编号"), "items": items, "total": len(items)}


@router.get("/{entry_id}/levels", response_model=dict)
def drill_level(
    entry_id: int,
    level: int | None = Query(default=None, ge=1, description="要展开到的层级，从 1 开始；不传时用路径默认展开层"),
    actor: Actor = Depends(get_actor),
) -> dict:
    """按层级展开下钻路径。

    只读动作，任何角色都可以调用。拒绝时返回 409 与结构化原因，
    ``保留上一层结果`` 里依旧是上次成功展开的层级，不会被清空。
    """
    target = level
    if target is None:
        path = service.get_entry(entry_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
        target = int(path.get("默认展开层") or 1)
    path, payload = service.drill(entry_id, target, actor=actor)
    if path is None:
        raise HTTPException(status_code=404, detail=payload.get("message"))
    if not payload.get("ok"):
        # 明确拒绝：用 409 表达「请求与当前模板版本/路径层级冲突」。
        raise HTTPException(status_code=409, detail=payload)
    return payload


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    actor: Actor = Depends(get_actor),
) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values, actor=actor)
    if entry is None:
        if isinstance(missing, str):
            return ActionResult(ok=False, message=missing)
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="下钻路径已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    actor: Actor = Depends(get_actor),
) -> ActionResult:
    """对单条下钻路径执行提交路径评审、启用路径、关闭路径；不允许的动作会被拦下并说明原因。"""
    action = str(payload.action or payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, actor=actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
