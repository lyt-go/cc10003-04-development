"""业务归属与角色权限。

聚合查询模板和多维下钻路径都带「业务归属」和「共享模板」两个属性：
- 管理员：可以查看和修改全部记录；
- 编辑：只能修改自己名下的记录，不能修改他人或共享记录；
- 只读：只能查看，任何修改动作都会被拒绝。

身份从请求头读取（X-Operator / X-Role），也兼容 X-User。前端在会话栏里切换
值班人和角色，请求封装会自动带上这两个头。当前仓库是内存演示版，所以这里
只做业务归属校验，不引入登录态；真实项目应把 Actor 换成认证中间件的结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Header

ROLE_ADMIN = "管理员"
ROLE_EDITOR = "编辑"
ROLE_VIEWER = "只读"
ROLES = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)

ROLE_ALIASES = {
    "管理员": ROLE_ADMIN,
    "admin": ROLE_ADMIN,
    "owner": ROLE_ADMIN,
    "编辑": ROLE_EDITOR,
    "editor": ROLE_EDITOR,
    "write": ROLE_EDITOR,
    "writer": ROLE_EDITOR,
    "只读": ROLE_VIEWER,
    "viewer": ROLE_VIEWER,
    "readonly": ROLE_VIEWER,
    "read-only": ROLE_VIEWER,
    "read": ROLE_VIEWER,
    "guest": ROLE_VIEWER,
}

SHARED_VALUES = {"共享", "公共", "全员", "shared", "public", "all"}
SHARED_KEY = "共享模板"
OWNER_KEY = "业务归属"


@dataclass(frozen=True)
class Actor:
    """一次请求的操作人身份。"""

    operator: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_viewer(self) -> bool:
        return self.role == ROLE_VIEWER

    def as_dict(self) -> dict[str, str]:
        return {"操作人": self.operator, "角色": self.role}


def normalize_role(raw: str | None) -> str:
    """把英文/别名角色统一成平台内的三种角色。未知角色原样保留。"""
    text = (raw or "").strip()
    if not text:
        return ROLE_EDITOR
    return ROLE_ALIASES.get(text, ROLE_ALIASES.get(text.lower(), text))


def _decode_header(raw: str | None) -> str:
    """HTTP 头只保证 latin-1：前端用 encodeURIComponent 传中文，这里还原。

    也兼容直接传 UTF-8 字节被按 latin-1 解码的情况，以及 ASCII 角色别名。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "%" in text:
        try:
            return unquote(text, encoding="utf-8").strip()
        except Exception:  # pragma: no cover - 非法编码时退回原值
            return text
    try:
        return text.encode("latin-1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def get_actor(
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_user: str | None = Header(default=None, alias="X-User"),
    x_role: str | None = Header(default=None, alias="X-Role"),
) -> Actor:
    """解析操作人；未带身份头时按「值班管理员 / 编辑」处理，兼容原有页面。"""
    operator = _decode_header(x_operator) or _decode_header(x_user) or "值班管理员"
    return Actor(operator=operator, role=normalize_role(_decode_header(x_role)))


def check_create(actor: Actor) -> str | None:
    """登记新记录前的权限校验。返回 None 表示允许。"""
    if actor.role not in ROLES:
        return f"角色「{actor.role}」未授权，无法创建记录"
    if actor.is_viewer:
        return "只读角色只能查看，不能创建或修改记录"
    return None


def check_modify(
    actor: Actor,
    row: dict,
    *,
    owner_key: str = OWNER_KEY,
    shared_key: str = SHARED_KEY,
) -> str | None:
    """修改已有记录前的业务归属校验。返回 None 表示允许。"""
    if actor.role not in ROLES:
        return f"角色「{actor.role}」未授权，不能执行修改动作"
    if actor.is_viewer:
        return "只读角色只能查看，不能改动记录"
    if actor.is_admin:
        return None

    owner = str(row.get(owner_key) or "").strip()
    shared = bool(row.get(shared_key)) or owner.lower() in SHARED_VALUES
    if shared:
        return f"「{row.get('模板编号') or row.get('路径编号') or '该记录'}」是共享记录，仅管理员可修改"
    if not owner:
        return "记录未设置业务归属，仅管理员可修改"
    if owner != actor.operator:
        return f"记录归属「{owner}」，{actor.operator} 无权修改他人记录"
    return None


def check_template_reference(actor: Actor, template: dict, *, action: str = "引用") -> str | None:
    """在他人的模板上登记/启用下钻路径也要受业务归属约束。"""
    template_label = template.get("模板编号") or template.get("id")
    if actor.role not in ROLES:
        return f"角色「{actor.role}」未授权，不能{action}模板 {template_label}"
    if actor.is_admin:
        return None
    if actor.is_viewer:
        return "只读角色只能查看，不能改动下钻路径"
    owner = str(template.get(OWNER_KEY) or "").strip()
    shared = bool(template.get(SHARED_KEY)) or owner.lower() in SHARED_VALUES
    if shared:
        return None
    if not owner:
        return f"模板 {template_label} 未设置业务归属，仅管理员可{action}"
    if owner != actor.operator:
        return f"模板 {template_label} 归属「{owner}」，{actor.operator} 无权在其上{action}"
    return None
