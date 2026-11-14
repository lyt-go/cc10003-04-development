"""聚合查询下钻快照的端到端校验。

不依赖 pytest/httpx：直接对 FastAPI 应用做函数级调用，覆盖
- 提交验证固化快照、两层下钻各自显示维度与聚合摘要；
- 模板换版后旧路径保留历史结果、新请求拒绝混用旧缓存；
- 路径缺少中间层级、引用已废弃模板、缓存摘要不一致时明确拒绝并保留上一层；
- 业务归属与只读角色的越权修改被拒绝；
- 查询缓存、下钻层级与模板状态同步更新，并有可核对的查询影响记录。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app import audit
from app.routers import datadrill as drill_router
from app.routers import queryboard as query_router
from app.schemas import EntryPayload
from app.security import Actor
from app.services.datadrill import DatadrillService
from app.services.queryboard import QueryboardService
from app.snapshots import (
    cache_entries,
    cache_integrity,
    current_version,
    lookup_cache,
    mark_cache_status,
    snapshot_integrity,
    versions_for,
)
from app.store import store

QB = QueryboardService()
DD = DatadrillService()
ADMIN = Actor("值班管理员", "管理员")
OTHER = Actor("其他分析员", "编辑")
VIEWER = Actor("只读访客", "只读")


def new_template(code: str, dims: list[str], measures: list[str], owner: str = "值班管理员", shared: bool = False):
    entry, missing = QB.create_entry(
        {"模板编号": code, "指标名称": f"{code} 指标", "维度字段": dims, "度量字段": measures,
         "业务归属": owner, "共享模板": shared},
        actor=ADMIN,
    )
    assert entry is not None, missing
    return store.find("queryboard", entry["id"])


def enable_template(entry, actor=ADMIN):
    _, msg = QB.run_action(entry["id"], "提交模板验证", actor=actor)
    assert "已提交" in msg, msg
    _, msg = QB.run_action(entry["id"], "启用模板", actor=actor)
    assert msg.startswith("查询模板已启用"), msg
    return entry


def new_path(code: str, template_code: str, dims: list[str], *, owner="值班管理员", shared=False,
             default_level=1, depth=None):
    entry, missing = DD.create_entry(
        {"路径编号": code, "关联模板": template_code, "起始维度": dims[0],
         "下钻层级": dims, "每层维度名": dims, "路径深度": depth or len(dims),
         "默认展开层": default_level, "业务归属": owner, "共享模板": shared},
        actor=ADMIN,
    )
    assert entry is not None, missing
    return store.find("datadrill", entry["id"])


def enable_path(entry, actor=ADMIN):
    _, msg = DD.run_action(entry["id"], "提交路径评审", actor=actor)
    assert "已提交" in msg, msg
    _, msg = DD.run_action(entry["id"], "启用路径", actor=actor)
    assert msg.startswith("下钻路径已启用"), msg
    return entry


def run() -> None:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {name}")
        else:
            failures.append(name)
            print(f"  FAIL {name} {detail}")

    # ---------------------------------------------------------------- 样例输入
    print("[样例] QUER-0002 日期+渠道，DATA-0002 默认展开到第二层")
    entry = store.find("queryboard", 2)
    enable_template(entry)
    path = store.find("datadrill", 2)
    enable_path(path)
    check("模板 v1 已固化", current_version("QUER-0002")["模板版本"] == 1)
    snapshot = current_version("QUER-0002")["快照"]
    check("快照维度=日期,渠道", snapshot["维度字段"] == ["日期", "渠道"])
    check("允许层级=2", snapshot["允许的下钻层级"] == 2)
    _, payload = DD.drill(path["id"], 2, actor=ADMIN)
    check("默认第二层展开成功", payload["ok"], payload.get("message", ""))
    levels = payload["层级结果"]
    check("返回两层", len(levels) == 2)
    check("第一层维度=日期", levels[0]["维度字段"] == "日期", str(levels[0]["维度字段"]))
    check("第二层维度=渠道", levels[1]["维度字段"] == "渠道", str(levels[1]["维度字段"]))
    check("两层度量不同", levels[0]["度量字段"] != levels[1]["度量字段"],
          f"{levels[0]['度量字段']} vs {levels[1]['度量字段']}")
    check("两层聚合摘要不同", levels[0]["摘要"] != levels[1]["摘要"])
    check("第二层累计维度两层", levels[1]["累计维度"] == ["日期", "渠道"])
    check("缓存两当前层", len([c for c in cache_entries("QUER-0002") if c["状态"] == "当前"]) == 2)

    # ---------------------------------------------------------------- 模板换版
    print("[换版] 模板切换维度后旧路径保留历史结果，新请求不得混用旧缓存")
    old_snapshot_digest = snapshot["摘要校验值"]
    old_path_version = path["关联模板版本"]
    entry["维度字段"] = "日期,渠道,设备类型"
    _, msg = QB.run_action(entry["id"], "提交模板验证", actor=ADMIN)
    check("换版提交验证生成 v2 待启用", "v2" in msg, msg)
    check("换版后模板仍为已启用", entry["status"] == "已启用", entry["status"])
    check("当前版本仍是 v1", current_version("QUER-0002")["模板版本"] == 1)
    pending = [v for v in versions_for("QUER-0002") if v["状态"] == "待启用"]
    check("v2 停在待启用", len(pending) == 1 and pending[0]["模板版本"] == 2)
    _, payload = DD.drill(path["id"], 2, actor=ADMIN)
    check("换版验证期间旧路径仍按 v1 展开", payload["ok"], payload.get("message", ""))
    check("换版验证期间读的仍是 v1", payload.get("模板版本") == 1)

    _, msg = QB.run_action(entry["id"], "启用模板", actor=ADMIN)
    check("启用 v2 成功", msg.startswith("查询模板已启用"), msg)
    check("启用后当前版本 v2", current_version("QUER-0002")["模板版本"] == 2)
    v1 = next(v for v in versions_for("QUER-0002") if v["模板版本"] == 1)
    check("v1 已标历史", v1["状态"] == "历史")
    check("v1 快照保留", v1["快照"]["摘要校验值"] == old_snapshot_digest)
    historical = [c for c in cache_entries("QUER-0002") if c["模板版本"] == 1]
    check("v1 缓存标历史", all(c["状态"] == "历史" for c in historical), str(historical))
    check("新缓存 v2 为当前", all(
        c["状态"] == "当前" for c in cache_entries("QUER-0002") if c["模板版本"] == 2
    ))
    check("旧缓存不参与新版本查找", lookup_cache("QUER-0002", 2, 1, status="历史") is None)
    for level in (1, 2):
        cache = lookup_cache("QUER-0002", old_path_version, level, status="历史")
        check(f"v1 第{level}层历史缓存保留", cache is not None)
        check(
            f"v1 第{level}层历史缓存与 v1 快照一致",
            cache_integrity(cache, v1["快照"], level, allow_historical=True) is None,
        )
    _, result = DD.drill(path["id"], 2, actor=ADMIN)
    check("旧路径新请求被拒绝", not result["ok"], result.get("message", ""))
    check("拒绝时提示换版", "换版" in result["message"], result["message"])
    check("拒绝时保留旧版历史结果", len(result["保留上一层结果"]) == 2,
          str(len(result["保留上一层结果"])))
    check("历史结果维度正确",
          [l["维度字段"] for l in result["保留上一层结果"]] == ["日期", "渠道"])
    check("模板状态仍为已启用", entry["status"] == "已启用")
    check("路径绑定版本仍为 v1，不混用 v2 缓存",
          result.get("路径绑定版本") == old_path_version, str(result.get("路径绑定版本")))
    _, bindings = DD.bindings(path["id"])
    historical_bindings = [b for b in bindings if b["状态"] == "历史"]
    check("历史绑定是 v1", bool(historical_bindings) and historical_bindings[0]["模板版本"] == old_path_version,
          str([b["模板版本"] for b in bindings]))
    check("旧绑定保留层级结果", bool(historical_bindings) and bool(historical_bindings[0]["层级结构"]))

    # ---------------------------------------------------------------- 重新启用后读新版本
    print("[恢复] 路径按 v2 修正并重新启用后只读 v2 缓存")
    path["每层维度名"] = "日期,渠道,设备类型"
    path["下钻层级"] = "日期,渠道,设备类型"
    path["路径深度"] = 3
    _, msg = DD.run_action(path["id"], "启用路径", actor=ADMIN)
    check("重新启用成功", msg.startswith("下钻路径已启用"), msg)
    check("绑定 v2", path["关联模板版本"] == 2)
    _, payload = DD.drill(path["id"], 3, actor=ADMIN)
    check("v2 三层展开成功", payload["ok"], payload.get("message", ""))
    check("返回三层", len(payload["层级结果"]) == 3)
    check("第三层维度=设备类型", payload["层级结果"][2]["维度字段"] == "设备类型")

    # ---------------------------------------------------------------- 缺少中间层级
    print("[缺层] 路径缺少任一中间层级必须明确拒绝并保留上一层结果")
    broken, broken_msg = DD.create_entry(
        {"路径编号": "DATA-TEST-01", "关联模板": "QUER-0002", "起始维度": "日期",
         "下钻层级": "日期,,设备类型", "每层维度名": "日期,,设备类型", "路径深度": 3,
         "默认展开层": 2, "业务归属": "值班管理员"}, actor=ADMIN,
    )
    check("登记时缺层被拒绝", broken is None and "缺少第2层" in str(broken_msg), str(broken_msg))
    raw = {
        "路径编号": "DATA-TEST-02", "关联模板": "QUER-0002", "起始维度": "日期",
        "下钻层级": "日期,设备类型", "每层维度名": "日期,设备类型",
        "路径深度": 3, "默认展开层": 3, "业务归属": "值班管理员",
    }
    created, missing = DD.create_entry(raw, actor=ADMIN)
    check("深度 3 但只声明 2 层被拒绝", created is None and isinstance(missing, str), str(missing))
    check("拒绝原因指明缺少第 3 层", "第3层" in str(missing), str(missing))

    partial = new_path("DATA-TEST-03", "QUER-0002", ["日期", "渠道", "设备类型"])
    enable_path(partial)
    check("部分路径启用后有三层", len(partial["层级结构"]) == 3)
    partial["层级结构"] = [
        dict(partial["层级结构"][0]),
        {**partial["层级结构"][1], "维度字段": ""},
        dict(partial["层级结构"][2]),
    ]
    _, result = DD.drill(partial["id"], 3, actor=ADMIN)
    check("中间层缺失时展开被拒绝", not result["ok"], result.get("message", ""))
    check("缺层提示明确", "缺少第2层" in result["message"], result["message"])
    check("保留第一层结果", len(result["保留上一层结果"]) == 1, str(len(result["保留上一层结果"])))
    check("阻断层级=2", result.get("阻断层级") == 2, str(result.get("阻断层级")))

    # ---------------------------------------------------------------- 已废弃模板
    print("[废弃] 引用已废弃模板的路径与展开请求必须拒绝")
    dep = new_template("QUER-TEST-DEP", ["日期", "渠道"], ["金额"])
    enable_template(dep)
    dep_path = new_path("DATA-TEST-DEP", "QUER-TEST-DEP", ["日期", "渠道"])
    enable_path(dep_path)
    _, msg = QB.run_action(dep["id"], "废弃模板", actor=ADMIN)
    check("模板已废弃", msg.startswith("查询模板已废弃"), msg)
    check("废弃后模板状态同步", dep["status"] == "已废弃")
    check("废弃后缓存标记", all(
        c["状态"] != "当前" for c in cache_entries("QUER-TEST-DEP")
    ), str([c["状态"] for c in cache_entries("QUER-TEST-DEP")]))
    _, result = DD.drill(dep_path["id"], 1, actor=ADMIN)
    check("已废弃模板展开被拒绝", not result["ok"], result.get("message", ""))
    check("提示模板已废弃", "已废弃" in result["message"], result["message"])
    _, missing = DD.create_entry(
        {"路径编号": "DATA-TEST-DEP2", "关联模板": "QUER-TEST-DEP", "起始维度": "日期",
         "下钻层级": "日期,渠道", "每层维度名": "日期,渠道", "路径深度": 2,
         "默认展开层": 1, "业务归属": "值班管理员"}, actor=ADMIN,
    )
    check("废弃模板不能登记新路径", isinstance(missing, str) and "已废弃" in missing, str(missing))

    # ---------------------------------------------------------------- 缓存摘要不一致
    print("[缓存] 摘要被篡改时拒绝并保留上一层结果")
    tamper_tpl = new_template("QUER-TEST-TAMPER", ["日期", "渠道"], ["金额"])
    enable_template(tamper_tpl)
    tamper_path = new_path("DATA-TEST-TAMPER", "QUER-TEST-TAMPER", ["日期", "渠道"])
    enable_path(tamper_path)
    level2 = lookup_cache("QUER-TEST-TAMPER", 1, 2)
    level2["摘要"] = "tampered-digest"
    _, result = DD.drill(tamper_path["id"], 2, actor=ADMIN)
    check("篡改缓存后展开被拒绝", not result["ok"], result.get("message", ""))
    check("提示缓存不一致", "不一致" in result["message"], result["message"])
    check("保留第 1 层结果", len(result["保留上一层结果"]) == 1)
    check("第 1 层结果维度正确", result["保留上一层结果"][0]["维度字段"] == "日期")
    level2["摘要"] = current_version("QUER-TEST-TAMPER")["快照"]["层级摘要"][1]["摘要"]
    _, result = DD.drill(tamper_path["id"], 2, actor=ADMIN)
    check("恢复摘要后可展开", result["ok"], result.get("message", ""))

    # ---------------------------------------------------------------- 快照自身一致性
    snap = current_version("QUER-TEST-TAMPER")["快照"]
    check("快照内部校验通过", snapshot_integrity(snap) is None)
    broken_snap = dict(snap)
    broken_snap["维度字段"] = ["日期"]
    check("快照被改动后校验失败", snapshot_integrity(broken_snap) is not None)

    # ---------------------------------------------------------------- 权限
    print("[权限] 业务归属与只读角色")
    mine = new_template("QUER-TEST-OWN", ["日期"], ["金额"], owner="其他分析员")
    _, msg = QB.run_action(mine["id"], "提交模板验证", actor=OTHER)
    check("归属人可以提交验证", "已提交" in msg, msg)
    _, msg = QB.run_action(mine["id"], "启用模板", actor=ADMIN)
    check("管理员可以启用他人模板", msg.startswith("查询模板已启用"), msg)
    denied_entry, msg = QB.run_action(mine["id"], "提交模板验证", actor=OTHER)
    check("归属人可对已启用模板提交新版本", "已提交" in msg, msg)
    _, msg = QB.run_action(mine["id"], "启用模板", actor=ADMIN)
    denied_entry, msg = QB.run_action(mine["id"], "废弃模板", actor=OTHER)
    check("归属人可废弃自己名下模板", msg.startswith("查询模板已废弃"), msg)
    denied_entry, msg = QB.run_action(mine["id"], "废弃模板", actor=Actor("路人", "编辑"))
    check("他人不能废弃该模板", denied_entry is None and "无权" in msg, msg)

    another = new_template("QUER-TEST-OTHER", ["日期"], ["金额"], owner="其他分析员")
    _, msg = QB.run_action(another["id"], "提交模板验证", actor=ADMIN)
    check("管理员可提交他人模板", "已提交" in msg, msg)
    denied_entry, msg = QB.run_action(another["id"], "提交模板验证", actor=VIEWER)
    check("只读角色不能修改", denied_entry is None and "只读" in msg, msg)
    denied_entry, msg = QB.run_action(another["id"], "提交模板验证", actor=Actor("路人", "编辑"))
    check("编辑不能修改他人模板", denied_entry is None and "无权" in msg, msg)
    denied_path, msg = DD.run_action(store.find("datadrill", 3)["id"], "启用路径", actor=VIEWER)
    check("只读角色不能改共享路径", denied_path is None and "只读" in msg, msg)
    shared_tpl = new_template("QUER-TEST-SHARED", ["日期"], ["金额"], owner="共享")
    denied_entry, msg = QB.run_action(shared_tpl["id"], "提交模板验证", actor=OTHER)
    check("共享模板仅管理员可改", denied_entry is None and "共享" in msg, msg)
    created, result = QB.create_entry(
        {"模板编号": "QUER-TEST-VIEWER", "指标名称": "越权", "维度字段": "日期", "度量字段": "金额"},
        actor=VIEWER,
    )
    check("只读角色不能创建模板", created is None and "只读" in result, str(result))
    other_path = new_path("DATA-TEST-OTHER", "QUER-TEST-OTHER", ["日期"], owner="其他分析员")
    denied_path, msg = DD.run_action(other_path["id"], "提交路径评审", actor=Actor("路人", "编辑"))
    check("编辑不能改他人路径", denied_path is None and "无权" in msg, msg)

    # 只读查看不受影响
    listed, total = QB.list_entries(size=100)
    check("只读角色仍可查看列表", total > 0)

    # ---------------------------------------------------------------- 查询影响记录
    print("[审计] 查询影响记录可核对")
    records, total = audit.list_entries(template_code="QUER-0002", limit=100)
    actions = {row["动作"] for row in records}
    check("记录包含提交验证", "提交模板验证" in actions, str(actions))
    check("记录包含启用模板", "启用模板" in actions, str(actions))
    check("记录包含层展开", any(row["动作"] == "层展开" for row in records))
    versions_seen = {row["模板版本"] for row in records if row["模板版本"]}
    check("记录覆盖 v1/v2", {1, 2}.issubset(versions_seen), str(versions_seen))
    rejects, _ = audit.list_entries(action="层展开", limit=100)
    check("拒绝流水可核对", any(row["结果"] == "拒绝" for row in rejects))

    # ---------------------------------------------------------------- 并发启用
    print("[并发] 同一模板并发启用不会出现两个当前版本")
    import threading

    conc = new_template("QUER-TEST-CONC", ["日期", "渠道"], ["金额"])
    QB.run_action(conc["id"], "提交模板验证", actor=ADMIN)
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            _, message = QB.run_action(conc["id"], "启用模板", actor=Actor(f"并发{index}", "管理员"))
            # 幂等返回同样表示状态收敛成功；只有出现第二版本或报错才算异常。
            if not (message.startswith("查询模板已启用") or "无需重复启用" in message):
                errors.append(message)
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    active = [row for row in versions_for("QUER-TEST-CONC") if row["状态"] == "当前"]
    check("并发后只有一个当前版本", len(active) == 1, str(len(active)))
    check("并发启用无异常", not errors, str(errors))

    # 并发下钻 + 启用不产生两个当前绑定
    conc_path = new_path("DATA-TEST-CONC", "QUER-TEST-CONC", ["日期", "渠道"])
    DD.run_action(conc_path["id"], "提交路径评审", actor=ADMIN)
    drill_errors: list[str] = []

    def drill_worker() -> None:
        try:
            DD.drill(conc_path["id"], 2, actor=ADMIN)
        except Exception as exc:  # pragma: no cover
            drill_errors.append(str(exc))

    def enable_worker() -> None:
        try:
            DD.run_action(conc_path["id"], "启用路径", actor=ADMIN)
        except Exception as exc:  # pragma: no cover
            drill_errors.append(str(exc))

    threads = [threading.Thread(target=drill_worker) for _ in range(6)]
    threads.append(threading.Thread(target=enable_worker))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    current_bindings = [
        row for row in store.rows("_drill_bindings")
        if row["路径ID"] == conc_path["id"] and row["状态"] == "当前"
    ]
    check("并发后只有一个当前绑定", len(current_bindings) == 1, str(len(current_bindings)))
    check("并发下钻/启用无异常", not drill_errors, str(drill_errors))

    # ---------------------------------------------------------------- 请求体不能改状态
    print("[入参] 状态与版本字段由服务端维护")
    forged, missing = QB.create_entry(
        {"模板编号": "QUER-TEST-FORGE", "指标名称": "伪造", "维度字段": "日期", "度量字段": "金额",
         "业务归属": "值班管理员", "status": "已启用", "模板版本": 99,
         "允许的下钻层级": 7, "快照摘要": "forged"},
        actor=ADMIN,
    )
    forged = store.find("queryboard", forged["id"])
    check("新建模板状态固定草稿", forged["status"] == "草稿", forged["status"])
    check("请求体不能注入版本", forged["模板版本"] == 0, str(forged["模板版本"]))
    forged_path, missing = DD.create_entry(
        {"路径编号": "DATA-TEST-FORGE", "关联模板": "QUER-TEST-FORGE", "起始维度": "日期",
         "下钻层级": "日期", "每层维度名": "日期", "路径深度": 1, "默认展开层": 1,
         "业务归属": "值班管理员", "status": "已启用", "关联模板版本": 99,
         "层级结构": [{"层级": 1, "维度字段": "日期"}]},
        actor=ADMIN,
    )
    forged_path = store.find("datadrill", forged_path["id"]) if forged_path else None
    check("新建路径状态固定草稿", forged_path is not None and forged_path["status"] == "草稿",
          str(forged_path and forged_path["status"]))
    check("请求体不能注入绑定版本",
          forged_path is not None and forged_path["关联模板版本"] == 0,
          str(forged_path and forged_path["关联模板版本"]))
    check("请求体不能注入层级结构",
          forged_path is not None and forged_path["层级结构"] == [],
          str(forged_path and forged_path["层级结构"]))

    # ---------------------------------------------------------------- 概览边界
    print("[边界] 内部表不出现在运营概览")
    module_names = store.module_names()
    check("概览没有内部表", all(not name.startswith("_") for name in module_names))
    check("queryboard 仍在概览", "queryboard" in module_names)
    overview = store.overview()
    check("概览模块数保持 18", len(overview["modules"]) == 18, str(len(overview["modules"])))

    # ---------------------------------------------------------------- 路由冒烟
    print("[路由] 快照/缓存/审计/层级接口")
    snapshot_payload = query_router.get_snapshot(2, version=None, include_results=True)
    check("快照接口返回维度", snapshot_payload["维度字段"] == ["日期", "渠道", "设备类型"])
    cache_payload = query_router.get_cache(2)
    check("缓存接口返回历史与当前", {i["状态"] for i in cache_payload["items"]} >= {"当前", "历史"})
    try:
        query_router.get_entry(9999)
        check("不存在模板返回 404", False)
    except HTTPException as exc:
        check("不存在模板返回 404", exc.status_code == 404)
    version_payload = query_router.list_versions(2)
    check("版本接口返回历史", any(v["状态"] == "历史" for v in version_payload["items"]))
    route_payload = drill_router.drill_level(2, level=None, actor=ADMIN)
    check("不传层级时使用默认展开层", route_payload["请求层级"] == path["默认展开层"],
          f"{route_payload['请求层级']} vs {path['默认展开层']}")
    # 冲突请求通过 HTTP 409 明确拒绝，并在 detail 中带回保留的上一层结果。
    old_path = store.find("datadrill", 2)
    store.find("queryboard", 2)["维度字段"] = "日期,渠道,设备类型"
    QB.run_action(2, "提交模板验证", actor=ADMIN)
    QB.run_action(2, "启用模板", actor=ADMIN)
    try:
        drill_router.drill_level(old_path["id"], level=2, actor=ADMIN)
        check("冲突展开返回 409", False)
    except HTTPException as exc:
        check("冲突展开返回 409", exc.status_code == 409)
        check("409 里保留上一层结果", "保留上一层结果" in (exc.detail or {}))

    print()
    if failures:
        print(f"FAILED {len(failures)}: {failures}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    run()
