"""多维下钻业务规则：路径评审、启用、层级展开与历史结果留存。

下钻路径启用时与当前模板版本绑定：只读取同一版本快照和同版本缓存，
模板换版后旧路径保留历史结果，但新的展开请求会被拒绝，必须重新启用路径。
"""
from __future__ import annotations

from typing import Any

from app import audit
from app.drill_state import (
    binding_rows,
    create_binding,
    mark_bindings_historical,
    template_lock,
)
from app.security import Actor, check_create, check_modify, check_template_reference
from app.snapshots import (
    SNAPSHOT_KEY,
    TEMPLATE_CODE_KEY,
    TEMPLATE_VERSION_KEY,
    cache_integrity,
    coerce_int,
    compact_levels,
    current_version,
    find_template,
    find_version_record,
    lookup_cache,
    snapshot_integrity,
    split_fields,
    template_display_code,
    utc_now,
)
from app.store import store

MODULE = "datadrill"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级", "关联模板"]
CREATABLE_FIELDS = [
    "路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量",
    "路径深度", "默认展开层", "关联看板", "关联模板",
    "业务归属", "共享模板", "备注",
]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已关闭"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "关闭路径": "已关闭"}
NEGATIVE_ACTIONS = ["关闭路径"]
ACTION_SOURCES = {
    "提交路径评审": {"草稿", "路径评审中"},
    "启用路径": {"路径评审中", "已启用"},
    "关闭路径": {"草稿", "路径评审中", "已启用"},
}


class DatadrillService:
    # ------------------------------------------------------------------
    # 列表与登记

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
                if keyword in str(row.get("路径编号", "")) or keyword in str(row.get("关联模板", ""))
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
        if missing:
            return None, missing

        template = find_template(values.get("关联模板"))
        if template is None:
            return None, f"关联模板「{values.get('关联模板')}」不存在"
        if str(template.get("status")) == "已废弃":
            return None, f"模板 {template_display_code(template)} 已废弃，不能登记下钻路径"
        denied = check_template_reference(actor, template, action="登记下钻路径")
        if denied:
            return None, denied

        path_code = str(values.get("路径编号")).strip()
        if any(str(row.get("路径编号")) == path_code for row in store.rows(MODULE)):
            return None, f"路径编号 {path_code} 已存在，请换一个编号"

        depth = coerce_int(values.get("路径深度") or values.get("下钻层级"))
        declared = split_fields(values.get("每层维度名"), keep_blank=True)
        if not declared:
            declared = split_fields(values.get("下钻层级"), keep_blank=True)
        if depth <= 0:
            depth = len([item for item in declared if item])
        if depth <= 0:
            return None, "路径深度为空，无法确定下钻层级"
        if len(declared) < depth:
            declared = declared + [""] * (depth - len(declared))
        declared = declared[:depth]
        missing_levels = [index + 1 for index, item in enumerate(declared) if not item.strip()]
        if missing_levels:
            return None, f"路径缺少第{'、'.join(str(item) for item in missing_levels)}层维度，不能登记"

        current = current_version(str(template.get(TEMPLATE_CODE_KEY)))
        snapshot = (current or {}).get(SNAPSHOT_KEY) or {}
        expected_dims = list(snapshot.get("维度字段") or [])
        if expected_dims and declared != expected_dims:
            return None, (
                f"路径层级与模板 {template_display_code(template)} "
                f"v{current.get(TEMPLATE_VERSION_KEY)} 快照维度不一致"
            )

        default_level = coerce_int(values.get("默认展开层"), default=1)
        if default_level < 1 or default_level > depth:
            return None, f"默认展开层必须在 1 到 {depth} 之间"

        measures = split_fields(values.get("每层度量"))
        if not measures and snapshot:
            measures = [
                str(level.get("度量字段"))
                for level in snapshot.get("层级摘要") or []
            ]
        if len(measures) < depth:
            measures = measures + [""] * (depth - len(measures))

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 状态、绑定版本、层级结构由服务端在启用时写入，请求体不能直接指定。
        for key in CREATABLE_FIELDS:
            if key in values:
                entry[key] = values.get(key)
        entry["路径编号"] = path_code
        entry["关联模板"] = str(template.get(TEMPLATE_CODE_KEY))
        entry["关联模板版本"] = 0
        entry["路径深度"] = depth
        entry["每层维度名"] = ",".join(declared)
        entry["每层度量"] = ",".join(item for item in measures[:depth] if item)
        entry["默认展开层"] = default_level
        entry["层级结构"] = []
        entry["业务归属"] = str(values.get("业务归属") or actor.operator).strip() or actor.operator
        entry["共享模板"] = _as_bool(values.get("共享模板")) or entry["业务归属"] in {"共享", "公共", "全员"}
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["创建人"] = actor.operator
        entry["更新时间"] = utc_now()
        rows.append(entry)
        audit.append(
            action="登记路径",
            result="成功",
            template_code=entry["关联模板"],
            path_id=entry["id"],
            path_code=path_code,
            operator=actor.operator,
            detail=f"声明 {depth} 层，默认展开到第 {default_level} 层",
        )
        return self._decorate(entry), []

    # ------------------------------------------------------------------
    # 状态流转

    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        actor = actor or Actor("值班管理员", "编辑")
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围"
        denied = check_modify(actor, entry)
        if denied:
            audit.append(
                action=action,
                result="拒绝",
                template_code=entry.get("关联模板"),
                path_id=entry.get("id"),
                path_code=entry.get("路径编号"),
                operator=actor.operator,
                detail=denied,
            )
            return None, denied

        if action == "提交路径评审":
            return self._submit_review(entry, actor)
        if action == "启用路径":
            return self._enable_path(entry, actor)
        return self._close_path(entry, actor)

    def _submit_review(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["提交路径评审"]:
            return None, f"路径当前状态「{source}」不能提交路径评审"

        template = find_template(entry.get("关联模板"))
        if template is None:
            return None, f"关联模板「{entry.get('关联模板')}」不存在或已归档"
        if str(template.get("status")) == "已废弃":
            return None, f"模板 {template_display_code(template)} 已废弃，不能提交路径评审"

        declared, missing = self._declared_levels(entry)
        if missing:
            return None, self._missing_level_message(missing)

        current = current_version(str(template.get(TEMPLATE_CODE_KEY)))
        if current is not None:
            snapshot = current.get(SNAPSHOT_KEY) or {}
            expected = list(snapshot.get("维度字段") or [])
            if expected and declared != expected:
                return None, (
                    f"路径层级（{','.join(declared)}）与模板 {template_display_code(template)} "
                    f"v{current.get(TEMPLATE_VERSION_KEY)} 快照维度（{','.join(expected)}）不一致，"
                    "请先按当前模板版本修正路径"
                )

        entry["status"] = ACTION_RULES["提交路径评审"]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["更新时间"] = utc_now()
        audit.append(
            action="提交路径评审",
            result="成功",
            template_code=entry.get("关联模板"),
            version=current.get(TEMPLATE_VERSION_KEY) if current else 0,
            path_id=entry.get("id"),
            path_code=entry.get("路径编号"),
            operator=actor.operator,
            detail="路径层级与模板当前快照一致"
            if current
            else "模板尚未固化快照，评审阶段只检查层级完整性",
        )
        return self._decorate(entry), "下钻路径已提交评审"

    def _enable_path(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["启用路径"]:
            return None, f"路径当前状态「{source}」不能启用"
        if source == "已启用":
            # 幂等：模板版本没变时不要重复绑定。
            template = find_template(entry.get("关联模板"))
            current = current_version(str(entry.get("关联模板"))) if template else None
            if (
                template is not None
                and current is not None
                and coerce_int(entry.get("关联模板版本")) == coerce_int(current.get(TEMPLATE_VERSION_KEY))
            ):
                return self._decorate(entry), "下钻路径已启用，当前绑定版本未变化"

        template = find_template(entry.get("关联模板"))
        if template is None:
            return None, f"关联模板「{entry.get('关联模板')}」不存在或已归档"
        template_code = str(template.get(TEMPLATE_CODE_KEY))
        if str(template.get("status")) == "已废弃":
            return self._reject_enable(entry, actor, f"模板 {template_display_code(template)} 已废弃，不能启用下钻路径")
        if str(template.get("status")) != "已启用":
            return self._reject_enable(
                entry,
                actor,
                f"模板 {template_display_code(template)} 状态为「{template.get('status')}」，"
                "请先启用模板再启用下钻路径",
            )
        denied = check_template_reference(actor, template, action="启用下钻路径")
        if denied:
            return self._reject_enable(entry, actor, denied)

        declared, missing = self._declared_levels(entry)
        if missing:
            return self._reject_enable(entry, actor, self._missing_level_message(missing))

        with template_lock(template_code):
            current = current_version(template_code)
            if current is None:
                return self._reject_enable(entry, actor, "模板尚未固化下钻快照，请先提交模板验证")
            version = coerce_int(current.get(TEMPLATE_VERSION_KEY))
            snapshot = current.get(SNAPSHOT_KEY) or {}
            integrity = snapshot_integrity(snapshot)
            if integrity:
                return self._reject_enable(entry, actor, f"模板 v{version} 快照校验失败：{integrity}")

            expected = list(snapshot.get("维度字段") or [])
            if declared != expected:
                return self._reject_enable(
                    entry,
                    actor,
                    f"路径层级（{','.join(declared)}）与模板 v{version} 快照维度"
                    f"（{','.join(expected)}）不一致，模板已切换维度字段，请修正路径后重新启用",
                )

            levels, error = self._resolve_levels(entry, template_code, version, snapshot, expected)
            if error:
                return self._reject_enable(entry, actor, error)

            if coerce_int(entry.get("默认展开层"), default=1) > len(levels):
                return self._reject_enable(
                    entry,
                    actor,
                    f"默认展开层 {entry.get('默认展开层')} 超过路径深度 {len(levels)}",
                )

            binding = create_binding(
                path_id=int(entry["id"]),
                path_code=entry.get("路径编号"),
                template_code=template_code,
                version=version,
                snapshot_digest=snapshot.get("摘要校验值"),
                levels=levels,
                operator=actor.operator,
            )

            entry["关联模板版本"] = version
            entry["快照摘要"] = snapshot.get("摘要校验值")
            entry["层级结构"] = levels
            entry["每层维度名"] = ",".join(expected)
            entry["每层度量"] = ",".join(str(level.get("度量字段")) for level in levels)
            entry["路径深度"] = len(expected)
            entry["status"] = ACTION_RULES["启用路径"]
            entry["pending"] = False
            entry["abnormal"] = False
            entry["启用人"] = actor.operator
            entry["启用时间"] = binding["启用时间"]
            entry["更新时间"] = binding["启用时间"]

        audit.append(
            action="启用路径",
            result="成功",
            template_code=template_code,
            version=version,
            path_id=entry.get("id"),
            path_code=entry.get("路径编号"),
            operator=actor.operator,
            detail=f"绑定模板 v{version}，可展开 {len(levels)} 层",
        )
        return self._decorate(entry), f"下钻路径已启用，只读取模板 v{version} 快照"

    def _close_path(
        self, entry: dict[str, Any], actor: Actor
    ) -> tuple[dict[str, Any] | None, str]:
        source = str(entry.get("status") or "")
        if source not in ACTION_SOURCES["关闭路径"]:
            return None, f"路径当前状态「{source}」不能关闭"
        template_code = str(entry.get("关联模板"))
        with template_lock(template_code):
            mark_bindings_historical(int(entry["id"]))
            entry["status"] = ACTION_RULES["关闭路径"]
            entry["pending"] = False
            entry["abnormal"] = True
            entry["关闭时间"] = utc_now()
            entry["更新时间"] = entry["关闭时间"]
        audit.append(
            action="关闭路径",
            result="成功",
            template_code=template_code,
            version=entry.get("关联模板版本"),
            path_id=entry.get("id"),
            path_code=entry.get("路径编号"),
            operator=actor.operator,
            detail="路径已关闭，历史绑定结果保留",
        )
        return self._decorate(entry), "下钻路径已关闭，历史结果保留"

    # ------------------------------------------------------------------
    # 层级展开

    def drill(
        self,
        entry_id: int,
        level: int,
        *,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """按层级展开：逐层校验缓存摘要，失败时保留上一层结果。"""
        actor = actor or Actor("值班管理员", "编辑")
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, _drill_failure(None, level, f"下钻路径 {entry_id} 不存在或已归档")

        template_code = str(entry.get("关联模板") or "")
        with template_lock(template_code):
            path = self._decorate(entry)
            if path.get("status") != "已启用":
                return path, _drill_failure(
                    path, level, f"路径状态为「{path.get('status')}」，仅已启用路径可以展开"
                )

            template = find_template(template_code)
            if template is None:
                return path, _drill_failure(
                    path, level, f"关联模板「{template_code}」不存在或已归档", preserved=path.get("层级结构")
                )
            if str(template.get("status")) == "已废弃":
                audit.append(
                    action="层展开", result="拒绝", template_code=template_code,
                    version=entry.get("关联模板版本"), path_id=entry.get("id"),
                    path_code=entry.get("路径编号"), level=level, operator=actor.operator,
                    detail="模板已废弃",
                )
                return path, _drill_failure(
                    path,
                    level,
                    f"模板 {template_display_code(template)} 已废弃，不能继续展开",
                    preserved=path.get("层级结构"),
                )
            if str(template.get("status")) != "已启用":
                return path, _drill_failure(
                    path,
                    level,
                    f"模板 {template_display_code(template)} 状态为「{template.get('status')}」，下钻路径读取不到启用中的模板版本",
                    preserved=path.get("层级结构"),
                )

            bound_version = coerce_int(entry.get("关联模板版本"))
            current = current_version(template_code)
            current_version_no = coerce_int(current.get(TEMPLATE_VERSION_KEY)) if current else 0
            if bound_version != current_version_no:
                audit.append(
                    action="层展开", result="拒绝", template_code=template_code,
                    version=bound_version, path_id=entry.get("id"),
                    path_code=entry.get("路径编号"), level=level, operator=actor.operator,
                    detail=f"模板当前版本 v{current_version_no}，路径绑定 v{bound_version}",
                )
                # 旧版本绑定和层级结果已转历史，这里回放历史结果作为「上一层」，
                # 同时明确拒绝用旧缓存拼接新版本请求。
                historical = self._historical_levels(int(entry["id"]), bound_version, level)
                return path, _drill_failure(
                    path,
                    level,
                    f"模板已换版为 v{current_version_no}，路径仍绑定 v{bound_version}，"
                    "请重新启用路径后再展开；历史结果可单独查看",
                    preserved=historical,
                    extra={
                        "当前模板版本": current_version_no,
                        "路径绑定版本": bound_version,
                        "历史结果": historical,
                    },
                )

            record = find_version_record(template_code, bound_version)
            snapshot = (record or {}).get(SNAPSHOT_KEY) or {}
            integrity = snapshot_integrity(snapshot)
            if integrity:
                return path, _drill_failure(
                    path, level, f"模板 v{bound_version} 快照校验失败：{integrity}",
                    preserved=path.get("层级结构"),
                )
            if str(entry.get("快照摘要") or "") != str(snapshot.get("摘要校验值") or ""):
                return path, _drill_failure(
                    path, level, "路径绑定的快照摘要与模板版本快照不一致",
                    preserved=path.get("层级结构"),
                )

            levels = path.get("层级结构") or []
            if level < 1 or level > len(levels) or level > int(entry.get("路径深度") or 0):
                return path, _drill_failure(
                    path, level, f"请求层级 {level} 超出路径深度 {entry.get('路径深度')}",
                    preserved=self._prefix(levels, len(levels)),
                )

            valid: list[dict[str, Any]] = []
            for expected_level in range(1, level + 1):
                node = next(
                    (item for item in levels if coerce_int(item.get("层级")) == expected_level),
                    None,
                )
                if node is None or not str(node.get("维度字段") or "").strip():
                    return path, _drill_failure(
                        path,
                        level,
                        self._missing_level_message([expected_level]),
                        preserved=self._prefix(valid, len(valid)),
                        extra={"阻断层级": expected_level},
                    )
                expected_dim = (snapshot.get("维度字段") or [])[expected_level - 1:expected_level]
                if expected_dim and node.get("维度字段") != expected_dim[0]:
                    return path, _drill_failure(
                        path,
                        level,
                        f"第{expected_level}层实际维度「{node.get('维度字段')}」与模板 v{bound_version} "
                        f"快照维度「{expected_dim[0]}」不一致，请重新启用路径",
                        preserved=self._prefix(valid, len(valid)),
                        extra={"阻断层级": expected_level},
                    )
                cache = lookup_cache(template_code, bound_version, expected_level)
                if cache is None:
                    return path, _drill_failure(
                        path,
                        level,
                        f"模板 v{bound_version} 第{expected_level}层缓存不存在",
                        preserved=self._prefix(valid, len(valid)),
                        extra={"阻断层级": expected_level},
                    )
                cache_error = cache_integrity(cache, snapshot, expected_level)
                if cache_error:
                    return path, _drill_failure(
                        path,
                        level,
                        f"{cache_error}，新请求不得混用旧缓存",
                        preserved=self._prefix(valid, len(valid)),
                        extra={"阻断层级": expected_level},
                    )
                valid.append({
                    "层级": expected_level,
                    "维度字段": cache.get("维度字段"),
                    "累计维度": cache.get("累计维度"),
                    "度量字段": cache.get("度量字段"),
                    "行数": cache.get("行数"),
                    "汇总": cache.get("汇总"),
                    "摘要": cache.get("摘要"),
                    "聚合结果": cache.get("聚合结果"),
                    "来源": f"模板 v{bound_version} 缓存",
                })

            audit.append(
                action="层展开",
                result="成功",
                template_code=template_code,
                version=bound_version,
                path_id=entry.get("id"),
                path_code=entry.get("路径编号"),
                level=level,
                operator=actor.operator,
                detail=f"展开 {level} 层，累计维度 {','.join(snapshot.get('维度字段') or [])}",
            )
            payload = {
                "ok": True,
                "message": f"已展开到第 {level} 层",
                "路径": path,
                "模板编号": template_code,
                "模板版本": bound_version,
                "请求层级": level,
                "路径深度": entry.get("路径深度"),
                "默认展开层": entry.get("默认展开层"),
                "层级结果": valid,
                "保留上一层结果": self._prefix(valid, max(len(valid) - 1, 0)),
                "当前模板版本": current_version_no,
            }
            return path, payload

    # ------------------------------------------------------------------
    # 历史与绑定

    def bindings(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, []
        rows = [
            row for row in binding_rows()
            if coerce_int(row.get("路径ID")) == entry_id
        ]
        rows.sort(key=lambda row: (coerce_int(row.get(TEMPLATE_VERSION_KEY)), coerce_int(row.get("id"))), reverse=True)
        items = []
        for row in rows:
            items.append({
                "路径编号": row.get("路径编号"),
                "模板编号": row.get(TEMPLATE_CODE_KEY),
                "模板版本": row.get(TEMPLATE_VERSION_KEY),
                "状态": row.get("状态"),
                "快照摘要": row.get("快照摘要"),
                "启用人": row.get("启用人"),
                "启用时间": row.get("启用时间"),
                "层级结构": compact_levels(row.get("层级结构")),
            })
        return self._decorate(entry), items

    def current_binding(self, entry_id: int) -> dict[str, Any] | None:
        for row in binding_rows():
            if coerce_int(row.get("路径ID")) == entry_id and row.get("状态") == "当前":
                return row
        return None

    def bind_existing(
        self,
        entry: dict[str, Any],
        version_record: dict[str, Any],
        *,
        operator: str,
    ) -> dict[str, Any] | None:
        """给已经处于启用状态的示例路径补一份同版本绑定（启动迁移用）。"""
        template_code = str(entry.get("关联模板") or "")
        version = coerce_int(version_record.get(TEMPLATE_VERSION_KEY))
        snapshot = version_record.get(SNAPSHOT_KEY) or {}
        expected = list(snapshot.get("维度字段") or [])
        levels, error = self._resolve_levels(entry, template_code, version, snapshot, expected)
        if error:
            return None
        binding = create_binding(
            path_id=int(entry["id"]),
            path_code=entry.get("路径编号"),
            template_code=template_code,
            version=version,
            snapshot_digest=snapshot.get("摘要校验值"),
            levels=levels,
            operator=operator,
        )
        entry["层级结构"] = levels
        entry["每层维度名"] = ",".join(expected)
        entry["每层度量"] = ",".join(str(level.get("度量字段")) for level in levels)
        entry["路径深度"] = len(expected)
        entry["默认展开层"] = min(
            coerce_int(entry.get("默认展开层"), default=1), len(expected)
        )
        return binding

    # ------------------------------------------------------------------
    # 辅助

    def _declared_levels(self, entry: dict[str, Any]) -> tuple[list[str], list[int]]:
        depth = coerce_int(entry.get("路径深度"))
        declared = split_fields(entry.get("每层维度名"), keep_blank=True)
        if not declared:
            declared = split_fields(entry.get("下钻层级"), keep_blank=True)
        if depth <= 0:
            depth = len([item for item in declared if item])
        if depth <= 0:
            return [], [1]
        if len(declared) < depth:
            declared = declared + [""] * (depth - len(declared))
        declared = declared[:depth]
        missing = [index + 1 for index, item in enumerate(declared) if not item.strip()]
        return declared, missing

    def _resolve_levels(
        self,
        entry: dict[str, Any],
        template_code: str,
        version: int,
        snapshot: dict[str, Any],
        expected_dims: list[str],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """按模板快照解析路径每一层，并逐层校验缓存摘要。"""
        levels: list[dict[str, Any]] = []
        for index, dimension in enumerate(expected_dims, start=1):
            cache = lookup_cache(template_code, version, index)
            if cache is None:
                return [], f"模板 v{version} 第{index}层缓存不存在，路径不能启用"
            error = cache_integrity(cache, snapshot, index)
            if error:
                return [], f"{error}，路径不能启用"
            levels.append({
                "层级": index,
                "维度字段": cache.get("维度字段"),
                "累计维度": cache.get("累计维度"),
                "度量字段": cache.get("度量字段"),
                "行数": cache.get("行数"),
                "汇总": cache.get("汇总"),
                "摘要": cache.get("摘要"),
                "聚合结果": cache.get("聚合结果"),
                "来源": f"模板 v{version} 缓存",
            })
        return levels, None

    def _prefix(self, levels: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        return [dict(level) for level in levels[:max(count, 0)]]

    def _historical_levels(
        self, path_id: int, version: int, up_to: int
    ) -> list[dict[str, Any]]:
        """回放路径在某个历史版本下已经固化的层级结果。"""
        for row in binding_rows():
            if (
                coerce_int(row.get("路径ID")) == path_id
                and coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version
            ):
                levels = list(row.get("层级结构") or [])
                return [
                    dict(level)
                    for level in levels
                    if coerce_int(level.get("层级")) <= up_to
                ]
        return []

    def _missing_level_message(self, missing: list[int]) -> str:
        return (
            f"路径缺少第{'、'.join(str(item) for item in missing)}层维度，"
            "中间层级必须完整，已保留上一层结果"
        )

    def _reject_enable(
        self, entry: dict[str, Any], actor: Actor, message: str
    ) -> tuple[dict[str, Any] | None, str]:
        audit.append(
            action="启用路径",
            result="拒绝",
            template_code=entry.get("关联模板"),
            version=entry.get("关联模板版本"),
            path_id=entry.get("id"),
            path_code=entry.get("路径编号"),
            operator=actor.operator,
            detail=message,
        )
        return None, message

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        payload = dict(entry)
        binding = self.current_binding(int(entry.get("id")))
        payload["当前绑定版本"] = binding.get(TEMPLATE_VERSION_KEY) if binding else None
        payload["绑定状态"] = binding.get("状态") if binding else None
        payload["可展开"] = bool(
            entry.get("status") == "已启用"
            and binding is not None
            and binding.get("状态") == "当前"
        )
        payload["层级摘要"] = compact_levels(payload.get("层级结构"))
        return payload


def _drill_failure(
    path: dict[str, Any] | None,
    level: int,
    message: str,
    *,
    preserved: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "message": message,
        "路径": path,
        "模板编号": path.get("关联模板") if path else None,
        "模板版本": path.get("关联模板版本") if path else None,
        "请求层级": level,
        "路径深度": path.get("路径深度") if path else None,
        "层级结果": [],
        "保留上一层结果": list(preserved or []),
    }
    payload.update(extra or {})
    return payload


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "共享"}
