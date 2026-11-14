"""模板级下钻快照：字段归一化、聚合摘要生成、摘要校验与缓存读取。

快照是聚合查询模板在「提交模板验证」时固化的不可变副本，包含：
- 维度字段；
- 度量字段；
- 允许的下钻层级（等于维度个数）；
- 每一层的确定性聚合结果和跨度摘要；
- 整个快照的摘要校验值。

下钻路径只读取与自己绑定的模板版本，缓存条目也按（模板编号、模板版本、层级）
定位，因此模板换版后旧缓存不会混进新请求。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from app.store import store

SNAPSHOT_MODULE = "_query_snapshots"
CACHE_MODULE = "_query_cache"
TEMPLATE_CODE_KEY = "模板编号"
TEMPLATE_VERSION_KEY = "模板版本"
SNAPSHOT_KEY = "快照"
SNAPSHOT_DIGEST_KEY = "摘要校验值"

_SPLIT_RE = re.compile(r"[\n\r,，、;；|]")
_DIMENSION_VALUES: dict[str, list[str]] = {
    "日期": ["2026-11-01", "2026-11-02", "2026-11-03"],
    "渠道": ["自然流量", "付费投放", "社交分享"],
    "支付方式": ["支付宝", "微信支付", "银行卡"],
    "地区": ["华东", "华南", "华北"],
    "设备类型": ["移动端", "桌面端", "小程序"],
    "新老客": ["新客", "老客"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def canonical(value: Any) -> str:
    """稳定序列化：同一份内容无论字典插入顺序如何都得到同一个摘要。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def short_digest(value: Any) -> str:
    return digest(value)[:16]


def split_fields(value: Any, *, keep_blank: bool = False) -> list[str]:
    """把维度/度量字段统一成字符串列表。

    支持列表、JSON 数组字符串和「日期,渠道」「日期、渠道」这类分隔写法。
    路径层级需要保留空位，才能在启用时明确指出缺少第几层。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(item).strip() if item is not None else "" for item in value]
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                raw = [str(item).strip() if item is not None else "" for item in parsed]
            else:
                raw = [item.strip() for item in _SPLIT_RE.split(text)]
        else:
            raw = [item.strip() for item in _SPLIT_RE.split(text)]
    if keep_blank:
        return raw
    return [item for item in raw if item]


def template_fingerprint(dimensions: Iterable[str], measures: Iterable[str]) -> str:
    """固化维度字段、度量字段（以及由维度推导出的层级）的字段指纹。"""
    dims = list(dimensions)
    return digest({"维度字段": dims, "度量字段": list(measures), "允许的下钻层级": len(dims)})


def _measure_for_level(measures: list[str], level: int) -> str:
    if not measures:
        return "记录数"
    return measures[min(level - 1, len(measures) - 1)]


def _dimension_values(dimension: str) -> list[str]:
    return _DIMENSION_VALUES.get(dimension, [f"{dimension}-A", f"{dimension}-B", f"{dimension}-C"])


def aggregate_rows(dimensions: list[str], measure: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """按当前累计维度生成确定性的聚合结果。

    平台当前没有接入真实数仓，这里用字段组合的稳定哈希生成可复现的数值，
    这样快照、缓存与下钻响应在重复请求时能逐字节比对摘要。
    """
    if not dimensions:
        return []
    groups: list[dict[str, str]] = [{}]
    for dimension in dimensions:
        next_groups: list[dict[str, str]] = []
        for group in groups:
            for value in _dimension_values(dimension):
                next_groups.append({**group, dimension: value})
        groups = next_groups
    rows: list[dict[str, Any]] = []
    for group in groups[:limit]:
        key = canonical({"维度": group, "度量": measure})
        number = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:10], 16)
        rows.append({
            "维度": group,
            "度量": measure,
            "聚合值": 1200 + number % 88000,
        })
    return rows


def build_snapshot(
    *,
    template_code: str,
    version: int,
    dimensions: list[str],
    measures: list[str],
    data_source: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """固化一份模板级下钻快照。"""
    generated_at = generated_at or utc_now()
    levels: list[dict[str, Any]] = []
    for index, dimension in enumerate(dimensions, start=1):
        measure = _measure_for_level(measures, index)
        rows = aggregate_rows(dimensions[:index], measure)
        level_payload = {
            "层级": index,
            "维度字段": dimension,
            "累计维度": dimensions[:index],
            "度量字段": measure,
            "行数": len(rows),
            "汇总": sum(int(row["聚合值"]) for row in rows),
            "聚合结果": rows,
        }
        level_payload["摘要"] = digest(level_payload)
        levels.append(level_payload)

    if not levels:
        raise ValueError("维度字段为空，无法生成下钻快照")

    deepest = levels[-1]
    snapshot: dict[str, Any] = {
        TEMPLATE_CODE_KEY: template_code,
        TEMPLATE_VERSION_KEY: version,
        "版本标识": f"{template_code}@v{version}",
        "维度字段": dimensions,
        "度量字段": measures,
        "允许的下钻层级": len(dimensions),
        "数据源": data_source or "",
        "生成时间": generated_at,
        "字段指纹": template_fingerprint(dimensions, measures),
        "层级摘要": levels,
        "聚合结果摘要": {
            "层级": deepest["层级"],
            "维度字段": deepest["维度字段"],
            "累计维度": deepest["累计维度"],
            "度量字段": deepest["度量字段"],
            "行数": deepest["行数"],
            "汇总": deepest["汇总"],
            "摘要": deepest["摘要"],
        },
    }
    snapshot[SNAPSHOT_DIGEST_KEY] = snapshot_digest(snapshot)
    return snapshot


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """按快照的字段指纹与每层摘要重新计算总摘要。"""
    return digest({
        "模板编号": snapshot.get(TEMPLATE_CODE_KEY),
        "模板版本": snapshot.get(TEMPLATE_VERSION_KEY),
        "字段指纹": snapshot.get("字段指纹"),
        "允许的下钻层级": snapshot.get("允许的下钻层级"),
        "层级摘要": [
            {
                "层级": level.get("层级"),
                "维度字段": level.get("维度字段"),
                "度量字段": level.get("度量字段"),
                "摘要": level.get("摘要"),
            }
            for level in snapshot.get("层级摘要") or []
        ],
    })


def snapshot_integrity(snapshot: dict[str, Any]) -> str | None:
    """校验快照内部一致性；不一致时返回可读原因。"""
    expected = snapshot_digest(snapshot)
    actual = str(snapshot.get(SNAPSHOT_DIGEST_KEY) or "")
    if not actual:
        return "快照缺少摘要校验值"
    if expected != actual:
        return "快照摘要校验值不一致"
    for level in snapshot.get("层级摘要") or []:
        payload = {key: value for key, value in level.items() if key != "摘要"}
        if digest(payload) != level.get("摘要"):
            return f"第{level.get('层级')}层聚合摘要不一致"
    if int(snapshot.get("允许的下钻层级") or 0) != len(snapshot.get("维度字段") or []):
        return "允许的下钻层级与维度字段数量不一致"
    return None


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, float):
            return int(value)
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _template_ref_value(ref: Any) -> str:
    if isinstance(ref, (int, float)):
        return str(int(ref))
    return str(ref or "").strip()


def find_template(ref: Any) -> dict[str, Any] | None:
    """按模板编号或记录 id 找到聚合查询模板。"""
    if ref is None:
        return None
    key = _template_ref_value(ref)
    if not key:
        return None
    for row in store.rows("queryboard"):
        if key in (str(row.get(TEMPLATE_CODE_KEY) or ""), str(row.get("id") or "")):
            return row
    return None


def path_template_ref(path: dict[str, Any]) -> Any:
    return path.get("关联模板") or path.get(TEMPLATE_CODE_KEY) or path.get("模板引用")


def template_display_code(template: dict[str, Any]) -> str:
    return str(template.get(TEMPLATE_CODE_KEY) or f"模板#{template.get('id')}")


# ---------------------------------------------------------------------------
# 模板版本与快照（内部表，不进入运营概览）


def template_versions() -> list[dict[str, Any]]:
    return store.rows(SNAPSHOT_MODULE)


def versions_for(template_code: str) -> list[dict[str, Any]]:
    return [
        row for row in template_versions()
        if str(row.get(TEMPLATE_CODE_KEY)) == template_code
    ]


def current_version(template_code: str) -> dict[str, Any] | None:
    for row in versions_for(template_code):
        if row.get("状态") == "当前":
            return row
    return None


def version_snapshot(template_code: str, version: int) -> dict[str, Any] | None:
    for row in versions_for(template_code):
        if coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version:
            return row.get(SNAPSHOT_KEY)
    return None


def next_version(template_code: str) -> int:
    return max(
        (coerce_int(row.get(TEMPLATE_VERSION_KEY)) for row in versions_for(template_code)),
        default=0,
    ) + 1


def store_snapshot(
    snapshot: dict[str, Any],
    *,
    operator: str,
    source_status: str,
    activate: bool = True,
) -> dict[str, Any]:
    """写入一条新版本快照。

    ``activate=False`` 时新版本停在「待启用」，当前版本继续服务；
    只有「启用模板」才会把待启用版本切换成当前版本，保证同一模板
    任何时刻最多只有一个当前版本。
    """
    template_code = str(snapshot[TEMPLATE_CODE_KEY])
    version = coerce_int(snapshot[TEMPLATE_VERSION_KEY])
    if activate:
        for row in versions_for(template_code):
            if row.get("状态") == "当前":
                row["状态"] = "历史"
    record = {
        "id": max((coerce_int(row.get("id")) for row in template_versions()), default=0) + 1,
        TEMPLATE_CODE_KEY: template_code,
        TEMPLATE_VERSION_KEY: version,
        "版本标识": snapshot.get("版本标识"),
        "状态": "当前" if activate else "待启用",
        "来源状态": source_status,
        "操作人": operator,
        "生成时间": snapshot.get("生成时间") or utc_now(),
        "字段指纹": snapshot.get("字段指纹"),
        "摘要校验值": snapshot.get(SNAPSHOT_DIGEST_KEY),
        SNAPSHOT_KEY: snapshot,
    }
    template_versions().append(record)
    return record


def pending_version(template_code: str) -> dict[str, Any] | None:
    """最新一份待启用快照。"""
    pending = [row for row in versions_for(template_code) if row.get("状态") == "待启用"]
    if not pending:
        return None
    return max(pending, key=lambda row: coerce_int(row.get(TEMPLATE_VERSION_KEY)))


def find_version_record(template_code: str, version: int) -> dict[str, Any] | None:
    for row in versions_for(template_code):
        if coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version:
            return row
    return None


# ---------------------------------------------------------------------------
# 查询缓存


def cache_rows() -> list[dict[str, Any]]:
    return store.rows(CACHE_MODULE)


def cache_entries(template_code: str) -> list[dict[str, Any]]:
    return [row for row in cache_rows() if str(row.get(TEMPLATE_CODE_KEY)) == template_code]


def lookup_cache(
    template_code: str,
    version: int,
    level: int,
    *,
    status: str | None = "当前",
) -> dict[str, Any] | None:
    """按模板版本与层级读取缓存；status=None 表示不限制缓存状态。"""
    for row in cache_rows():
        if (
            str(row.get(TEMPLATE_CODE_KEY)) == template_code
            and coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version
            and coerce_int(row.get("层级")) == level
            and (status is None or row.get("状态") == status)
        ):
            return row
    return None


def activate_version(template_code: str, version: int) -> dict[str, Any] | None:
    """把指定版本切成当前版本，其余版本标历史；返回切到当前的记录。"""
    target = None
    for row in versions_for(template_code):
        if coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version:
            row["状态"] = "当前"
            target = row
        elif row.get("状态") == "当前":
            row["状态"] = "历史"
    return target


def build_cache_entries(snapshot: dict[str, Any], *, status: str = "当前") -> list[dict[str, Any]]:
    """为快照的每一层生成可复用缓存；重复调用会复用已存在的同版本条目。"""
    template_code = str(snapshot[TEMPLATE_CODE_KEY])
    version = coerce_int(snapshot[TEMPLATE_VERSION_KEY])
    entries: list[dict[str, Any]] = []
    existing = [
        row for row in cache_rows()
        if str(row.get(TEMPLATE_CODE_KEY)) == template_code
        and coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version
    ]
    for level in snapshot.get("层级摘要") or []:
        level_no = coerce_int(level.get("层级"))
        row = next((item for item in existing if coerce_int(item.get("层级")) == level_no), None)
        if row is None:
            row = {
                "id": max((coerce_int(item.get("id")) for item in cache_rows()), default=0) + 1,
                TEMPLATE_CODE_KEY: template_code,
                TEMPLATE_VERSION_KEY: version,
                "层级": level_no,
                "维度字段": level.get("维度字段"),
                "累计维度": level.get("累计维度"),
                "度量字段": level.get("度量字段"),
                "摘要": level.get("摘要"),
                "聚合结果": level.get("聚合结果"),
                "行数": level.get("行数"),
                "汇总": level.get("汇总"),
                "生成时间": snapshot.get("生成时间"),
            }
            cache_rows().append(row)
        row["状态"] = status
        entries.append(row)
    return entries


def mark_cache_historical(template_code: str, keep_version: int | None = None) -> int:
    """把非当前版本的缓存标成历史，保留条目供旧路径核对历史结果。"""
    changed = 0
    for row in cache_rows():
        if str(row.get(TEMPLATE_CODE_KEY)) != template_code:
            continue
        if keep_version is not None and coerce_int(row.get(TEMPLATE_VERSION_KEY)) == keep_version:
            continue
        if row.get("状态") == "当前":
            row["状态"] = "历史"
            changed += 1
    return changed


def mark_cache_status(template_code: str, version: int, status: str) -> int:
    changed = 0
    for row in cache_rows():
        if (
            str(row.get(TEMPLATE_CODE_KEY)) == template_code
            and coerce_int(row.get(TEMPLATE_VERSION_KEY)) == version
            and row.get("状态") != status
        ):
            row["状态"] = status
            changed += 1
    return changed


def cache_integrity(
    cache: dict[str, Any],
    snapshot: dict[str, Any],
    level: int,
    *,
    allow_historical: bool = False,
    allow_pending: bool = False,
) -> str | None:
    """缓存条目与对应层快照的摘要比对。"""
    level_meta = next(
        (item for item in snapshot.get("层级摘要") or [] if coerce_int(item.get("层级")) == level),
        None,
    )
    if level_meta is None:
        return f"模板快照缺少第{level}层"
    allowed = {"当前"}
    if allow_historical:
        allowed.add("历史")
    if allow_pending:
        allowed.add("待启用")
    cache_status = cache.get("状态")
    if cache_status not in allowed:
        return f"第{level}层缓存状态为「{cache_status}」，不可用于当前读取"
    if cache.get("摘要") != level_meta.get("摘要"):
        return f"第{level}层缓存摘要与模板快照不一致"
    if canonical(cache.get("聚合结果")) != canonical(level_meta.get("聚合结果")):
        return f"第{level}层缓存聚合结果与模板快照不一致"
    return None


def compact_levels(levels: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """给接口返回的层级摘要，去掉冗余结构但保留实际维度、度量和结果。"""
    compact: list[dict[str, Any]] = []
    for level in levels or []:
        compact.append({
            "层级": level.get("层级"),
            "维度字段": level.get("维度字段"),
            "累计维度": level.get("累计维度"),
            "度量字段": level.get("度量字段"),
            "行数": level.get("行数"),
            "汇总": level.get("汇总"),
            "摘要": level.get("摘要"),
            "聚合结果": level.get("聚合结果"),
        })
    return compact
