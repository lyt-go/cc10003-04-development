"""聚合查询业务规则：状态流转、字段校验、模板版本与下钻快照都收在这里。

关键约束：
- 「提交模板验证」时固化维度字段、度量字段和允许的下钻层级，生成模板级快照；
- 「启用模板」只启用已经验证过的快照，并把同版本缓存标成当前；
- 同一模板的启用动作串行执行，保证不会同时存在两个当前版本；
- 模板换版后旧版本快照与旧缓存保留历史，新请求不得读取旧版本缓存。
"""
from __future__ import annotations

from typing import Any

from app import audit
from app.drill_state import invalidate_bindings_for_template, template_lock
from app.security import (
    Actor,
    check_create,
    check_modify,
    check_template_reference,
)
from app.snapshots import (
    SNAPSHOT_KEY,
    TEMPLATE_VERSION_KEY,
    activate_version,
    build_cache_entries,
    build_snapshot,
    cache_entries,
    cache_integrity,
    compact_levels,
    current_version,
    find_version_record,
    lookup_cache,
    mark_cache_historical,
    mark_cache_status,
    next_version,
    pending_version,
    snapshot_integrity,
    split_fields,
    store_snapshot,
    template_display_code,
    utc_now,
    versions_for,
)
from app.store import store

MODULE = "queryboard"
REQUIRED_FIELDS = ["模板编号", "指标名称", "维度字段"]
CREATABLE_FIELDS = [
    "模板编号", "指标名称", "维度字段", "度量字段", "排序规则",
    "分页上限", "缓存秒数", "数据源", "业务归属", "共享模板", "备注",
]
STATUS_ORDER = ["草稿", "模板验证中", "已启用", "已废弃"]
ACTION_RULES = {"提交模板验证": "模板验证中", "启用模板": "已启用", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = ["废弃模板"]

# 模板状态机：只允许从草稿提交验证，验证通过后启用，已启用或验证中的模板可以废弃。
ACTION_SOURCES = {
    # 已启用模板可以再次提交验证，用于换版；新版本在启用前不接管当前流量。
    "提交模板验证": {"草稿", "模板验证中", "已启用"},
    "启用模板": {"模板验证中", "已启用"},
    "废弃模板": {"草稿", "模板验证中", "已启用"},
}

class QueryboardService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("模板编号", "")) or keyword in str(row.get("指标名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._decorate(entry) if entry else None

    def create_entry(
        self,
        values: dict[str, Any],
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, list[str] | str]:
        actor = actor or Actor("值班管理员", "编辑")
        denied = check_create(actor)
        if denied:
            return None, denied

        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        dimensions = split_fields(values.get("维度字段"))
        measures = split_fields(values.get("度量字段"))
        if not dimensions:
            missing.append("维度字段")
        if not measures:
            missing.append("度量字段")
        missing = list(dict.fromkeys(missing))
        if missing:
            return None, missing

        template_code = str(values.get("模板编号")).strip()
        if any(str(row.get("模板编号")) == template_code for row in store.rows(MODULE)):
            return None, f"模板编号 {template_code} 已存在，请换一个编号"

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 只接收模板字段，状态、版本、快照摘要一律由服务端维护，禁止请求体直接指定。
        for key in CREATABLE_FIELDS:
            if key in values:
                entry[key] = values.get(key)
        entry["模板编号"] = template_code
        entry["维度字段"] = ",".join(dimensions)
        entry["度量字段"] = ",".join(measures)
        entry["业务归属"] = str(values.get("业务归属") or actor.operator).strip() or actor.operator
        entry["共享模板"] = _as_bool(values.get("共享模板")) or entry["业务归属"] in {"共享", "公共", "全员"}
        entry["模板版本"] = 0
        entry["允许的下钻层级"] = len(dimensions)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["创建人"] = actor.operator
        entry["更新时间"] = utc_now()
        rows.append(entry)
        audit.append(
            action="登记模板",
            result="成功",
            template_code=template_code,
            version=0,
            operator=actor.operator,
            detail=f"维度 {len(dimensions)} 个，度量 {len(measures)} 个",
        )
        return self._decorate(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        actor = actor or Actor("值班管理员", "编辑")
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合查询可执行范围"
        denied = check_modify(actor, entry)
        if denied:
            audit.append(
                action=action,
                result="拒绝",
                template_code=entry.get("模板编号"),
                version=entry.get("模板版本"),
                operator=actor.operator,
                detail=denied,
            )
            return None, denied

        if action == "提交模板验证":
            return self._submit_for_validation(entry, actor)
        if action == "启用模板":
            return self._enable_template(entry, actor)
        return self._deprecate_template(entry, actor)

    # ------------------------------------------------------------------
    # 动作实现

    def _submit_for_validation(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["提交模板验证"]:
            return None, f"模板当前状态「{source}」不能提交模板验证"

        dimensions = split_fields(entry.get("维度字段"))
        measures = split_fields(entry.get("度量字段"))
        if not dimensions:
            return None, "维度字段为空，无法生成下钻快照"
        if not measures:
            return None, "度量字段为空，无法生成下钻快照"
        if len(dimensions) > 8:
            return None, "维度字段超过 8 个，请拆分为多条下钻路径"

        template_code = str(entry.get("模板编号"))
        with template_lock(template_code):
            version = next_version(template_code)
            generated_at = utc_now()
            snapshot = build_snapshot(
                template_code=template_code,
                version=version,
                dimensions=dimensions,
                measures=measures,
                data_source=str(entry.get("数据源") or ""),
                generated_at=generated_at,
            )
            integrity = snapshot_integrity(snapshot)
            if integrity:
                return None, f"下钻快照生成失败：{integrity}"

            # 同一个模板同时只保留一份待启用快照：若上一次提交验证后没有启用，
            # 这次提交会取代它，避免积压多个待启用版本。
            superseded = pending_version(template_code)
            if superseded is not None:
                superseded["状态"] = "已被替换"
                mark_cache_status(
                    template_code,
                    int(superseded.get(TEMPLATE_VERSION_KEY) or 0),
                    "已被替换",
                )

            # 新版本先停在「待启用」，当前版本继续服务；只有启用动作才切换当前版本。
            # 缓存与版本记录的写入在同一临界区内完成，状态不会彼此脱节。
            build_cache_entries(snapshot, status="待启用")
            store_snapshot(
                snapshot,
                operator=actor.operator,
                source_status=source,
                activate=False,
            )

            entry["维度字段"] = ",".join(dimensions)
            entry["度量字段"] = ",".join(measures)
            entry["模板版本"] = version
            entry["版本标识"] = snapshot["版本标识"]
            entry["允许的下钻层级"] = snapshot["允许的下钻层级"]
            entry["快照摘要"] = snapshot["摘要校验值"]
            entry["快照生成时间"] = generated_at
            entry["待启用版本"] = version
            if source == "已启用" and current_version(template_code) is not None:
                # 已启用模板换版验证期间，旧版本继续对外服务，下钻路径不受影响。
                entry["status"] = "已启用"
                entry["pending"] = False
            else:
                entry["status"] = ACTION_RULES["提交模板验证"]
                entry["pending"] = True
            entry["abnormal"] = False
            entry["更新时间"] = generated_at

        audit.append(
            action="提交模板验证",
            result="成功",
            template_code=template_code,
            version=version,
            operator=actor.operator,
            detail=f"固化 {len(dimensions)} 层维度、{len(measures)} 个度量",
        )
        return self._decorate(entry), f"查询模板已提交模板验证，下钻快照 v{version} 已固化"

    def _enable_template(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["启用模板"]:
            return None, f"模板当前状态「{source}」不能启用，请先提交模板验证固化快照"

        template_code = str(entry.get("模板编号"))
        with template_lock(template_code):
            version = int(entry.get("模板版本") or 0)
            if version <= 0:
                return None, "模板尚未固化下钻快照，请先提交模板验证"
            record = find_version_record(template_code, version)
            if record is None:
                return None, f"模板版本 v{version} 的快照不存在，请重新提交模板验证"
            if source == "已启用" and record.get("状态") == "当前":
                # 幂等：同一版本已启用且版本一致时直接返回，不再重复切换。
                return self._decorate(entry), "查询模板已处于启用状态，无需重复启用"

            snapshot = record.get(SNAPSHOT_KEY) or {}
            integrity = snapshot_integrity(snapshot)
            if integrity:
                return None, f"模板版本 v{version} 快照校验失败：{integrity}"
            if str(entry.get("快照摘要") or "") != str(snapshot.get("摘要校验值") or ""):
                return None, "模板记录的快照摘要与版本快照不一致，请重新提交模板验证"

            # 先核对同版本缓存，全部一致才切换版本；任何一层摘要漂移都拒绝启用。
            existing = [
                row for row in cache_entries(template_code)
                if int(row.get(TEMPLATE_VERSION_KEY) or 0) == version
            ]
            if existing:
                for level in snapshot.get("层级摘要") or []:
                    level_no = int(level.get("层级") or 0)
                    cache = lookup_cache(template_code, version, level_no, status=None)
                    if cache is None:
                        build_cache_entries(snapshot, status="待启用")
                        continue
                    error = cache_integrity(cache, snapshot, level_no, allow_historical=False, allow_pending=True)
                    if error:
                        audit.append(
                            action="启用模板",
                            result="拒绝",
                            template_code=template_code,
                            version=version,
                            operator=actor.operator,
                            detail=error,
                        )
                        return None, f"{error}，请重新提交模板验证重建缓存"
            else:
                build_cache_entries(snapshot, status="待启用")

            # 状态切换：旧当前版本转历史、旧当前缓存转历史、新版本及其缓存转当前。
            # 绑定到旧版本的路径同步转历史并保留层级结果，之后的新展开请求会被拒绝。
            activate_version(template_code, version)
            mark_cache_historical(template_code, keep_version=version)
            mark_cache_status(template_code, version, "当前")
            invalidated = invalidate_bindings_for_template(template_code, version)

            entry["status"] = ACTION_RULES["启用模板"]
            entry["pending"] = False
            entry["abnormal"] = False
            entry["启用人"] = actor.operator
            entry["启用时间"] = utc_now()
            entry["更新时间"] = entry["启用时间"]

        audit.append(
            action="启用模板",
            result="成功",
            template_code=template_code,
            version=version,
            operator=actor.operator,
            detail=(
                f"当前版本切换为 v{version}"
                + (f"，{invalidated} 条旧版本路径绑定转历史" if invalidated else "")
            ),
        )
        return self._decorate(entry), f"查询模板已启用，下钻只读取 v{version} 快照"

    def _deprecate_template(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["废弃模板"]:
            return None, f"模板当前状态「{source}」不能废弃"
        template_code = str(entry.get("模板编号"))
        version = int(entry.get("模板版本") or 0)
        with template_lock(template_code):
            mark_cache_status(template_code, version, "已废弃")
            current = current_version(template_code)
            if current is not None and int(current.get(TEMPLATE_VERSION_KEY) or 0) == version:
                current["状态"] = "已废弃"
            entry["status"] = ACTION_RULES["废弃模板"]
            entry["pending"] = False
            entry["abnormal"] = True
            entry["废弃时间"] = utc_now()
            entry["更新时间"] = entry["废弃时间"]
        audit.append(
            action="废弃模板",
            result="成功",
            template_code=template_code,
            version=version,
            operator=actor.operator,
            detail="模板已废弃，引用它的下钻路径将被拒绝",
        )
        return self._decorate(entry), "查询模板已废弃，旧下钻路径保留历史结果但不再可展开"

    # ------------------------------------------------------------------
    # 快照与缓存查询

    def versions(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        template_code = str(entry.get("模板编号"))
        versions = []
        for row in sorted(
            versions_for(template_code),
            key=lambda item: int(item.get(TEMPLATE_VERSION_KEY) or 0),
            reverse=True,
        ):
            snapshot = row.get(SNAPSHOT_KEY) or {}
            versions.append({
                "模板编号": template_code,
                "模板版本": row.get(TEMPLATE_VERSION_KEY),
                "版本标识": row.get("版本标识"),
                "状态": row.get("状态"),
                "来源状态": row.get("来源状态"),
                "生成时间": row.get("生成时间"),
                "维度字段": snapshot.get("维度字段"),
                "度量字段": snapshot.get("度量字段"),
                "允许的下钻层级": snapshot.get("允许的下钻层级"),
                "字段指纹": snapshot.get("字段指纹"),
                "摘要校验值": snapshot.get("摘要校验值"),
                "聚合结果摘要": snapshot.get("聚合结果摘要"),
                "层级摘要": compact_levels(snapshot.get("层级摘要")),
            })
        return entry, versions

    def snapshot_for(
        self,
        entry_id: int,
        *,
        version: int | None = None,
        include_results: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """读取指定模板版本的快照；不传版本时取当前版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None, f"查询模板 {entry_id} 不存在或已归档"
        template_code = str(entry.get("模板编号"))
        if version is None:
            record = current_version(template_code)
            if record is None:
                return entry, None, "模板尚未固化管理快照，请先提交模板验证"
        else:
            record = find_version_record(template_code, version)
            if record is None:
                return entry, None, f"模板版本 v{version} 不存在"
        snapshot = record.get(SNAPSHOT_KEY) or {}
        integrity = snapshot_integrity(snapshot)
        if integrity:
            return entry, None, f"模板版本 v{version if version is not None else record.get(TEMPLATE_VERSION_KEY)} 快照校验失败：{integrity}"
        payload = {
            "模板编号": template_code,
            "模板版本": record.get(TEMPLATE_VERSION_KEY),
            "版本标识": record.get("版本标识"),
            "版本状态": record.get("状态"),
            "维度字段": snapshot.get("维度字段"),
            "度量字段": snapshot.get("度量字段"),
            "允许的下钻层级": snapshot.get("允许的下钻层级"),
            "字段指纹": snapshot.get("字段指纹"),
            "摘要校验值": snapshot.get("摘要校验值"),
            "生成时间": snapshot.get("生成时间"),
            "聚合结果摘要": snapshot.get("聚合结果摘要"),
            "层级摘要": compact_levels(snapshot.get("层级摘要")) if include_results else [
                {key: value for key, value in level.items() if key != "聚合结果"}
                for level in compact_levels(snapshot.get("层级摘要"))
            ],
        }
        return entry, payload, None

    def cache_summary(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        template_code = str(entry.get("模板编号"))
        rows = sorted(
            cache_entries(template_code),
            key=lambda row: (int(row.get(TEMPLATE_VERSION_KEY) or 0), int(row.get("层级") or 0)),
            reverse=True,
        )
        return entry, [
            {
                "模板编号": row.get("模板编号"),
                "模板版本": row.get("模板版本"),
                "层级": row.get("层级"),
                "维度字段": row.get("维度字段"),
                "累计维度": row.get("累计维度"),
                "度量字段": row.get("度量字段"),
                "状态": row.get("状态"),
                "摘要": row.get("摘要"),
                "行数": row.get("行数"),
                "汇总": row.get("汇总"),
                "生成时间": row.get("生成时间"),
            }
            for row in rows
        ]

    def ensure_reference_allowed(
        self, entry: dict[str, Any], actor: Actor, *, action: str = "登记下钻路径"
    ) -> str | None:
        """下钻路径登记/启用时复用模板的业务归属校验。"""
        if str(entry.get("status")) == "已废弃":
            return f"模板 {template_display_code(entry)} 已废弃，不能{action}"
        return check_template_reference(actor, entry, action=action)

    def template_by_ref(self, ref: Any) -> dict[str, Any] | None:
        from app.snapshots import find_template
        return find_template(ref)

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表页需要看到当前版本是否可以下钻，以及快照边界。"""
        payload = dict(entry)
        template_code = str(entry.get("模板编号"))
        record = current_version(template_code)
        snapshot = (record or {}).get(SNAPSHOT_KEY) or {}
        pending = pending_version(template_code)
        payload["当前快照版本"] = record.get(TEMPLATE_VERSION_KEY) if record else None
        payload["当前快照状态"] = record.get("状态") if record else None
        payload["待启用快照版本"] = pending.get(TEMPLATE_VERSION_KEY) if pending else None
        payload["允许的下钻层级"] = snapshot.get("允许的下钻层级")
        payload["快照维度字段"] = snapshot.get("维度字段")
        payload["快照度量字段"] = snapshot.get("度量字段")
        payload["快照摘要"] = snapshot.get("摘要校验值")
        payload["可下钻"] = (
            str(entry.get("status")) == "已启用"
            and record is not None
            and record.get("状态") == "当前"
        )
        return payload


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "共享"}
