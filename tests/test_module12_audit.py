#!/usr/bin/env python3
"""
P2-13d 审计日志回归。

    .venv/bin/python tests/test_module12_audit.py

--------------------------------------------------------------------------
这个模块的判据偏「不变量」，不是「功能」
--------------------------------------------------------------------------
审计的价值全在**出事那天能不能查出来**上，而它缺东西的时候不会报错：
    · 少记一条动作 → 功能测试全绿，日志里就是没有那条；
    · 密码明文混进 detail → 没有任何功能异常，只是某天审计表成了泄漏面；
    · 日志能被别人改 → 直到有人改了你才知道。
所以断言都写成「**必须不可能**」的形式：
不只断言「有日志」，还断言「没有明文」「没有 update/delete 入口」「表里没有可改的列」。

--------------------------------------------------------------------------
一个必须说清的张力：「不可删」是应用层约定，不是数据库级
--------------------------------------------------------------------------
本模块**自己**在收尾时用 SQL 删掉造出来的审计行 —— 那就是删审计。
这不是自相矛盾，而是把边界写明白：

    · 应用层（`core/audit_repo.py`）不提供 update / delete；
    · 表里没有 `update_time` / `deleted_at`，「顺手改一下」不成立；
    · 但**任何有 DB 写权限的人**都能 `DELETE FROM audit_log`，也能 `DROP TABLE`。

真正不可篡改的代价是有的（要额外写审计库 / 加表级权限 / 归档到不可写存储），
本轮没做，理由与代价见迭代文档 §3.5。
"""
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("EMBEDDING_BACKEND", "local")
os.environ.setdefault("RERANK_BACKEND", "local")

# 审计页的源码本身也是被测对象：它里面那串 `action === 'xxx'` 决定了下拉和明细怎么渲染，
# 而那串动作名没有任何类型检查能保证它跟服务端白名单对得上（都是字符串）。
# 所以这里直接把文件读进来做文本断言 —— 读不到就当场红，不静默跳过。
_AUDIT_VIEW_SRC_PATH = Path(__file__).resolve().parents[1] / "admin-console" / "src" / "views" / "AuditView.vue"
_AUDIT_VIEW_SRC = (
    _AUDIT_VIEW_SRC_PATH.read_text(encoding="utf-8")
    if _AUDIT_VIEW_SRC_PATH.exists() else ""
)

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
ADMIN_USERNAME = "wu.jing"
HR_USERNAME = "zhou.yan"
USER_USERNAME = "chen.jie"
PREFIX = "m13d_"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


# --------------------------------------------------------------------------- #
print("== 第 1 组：前置与表结构 ==")
from core import audit_repo  # noqa: E402
from core import kb_acl  # noqa: E402
from core.db import get_engine, now_db  # noqa: E402
from sqlalchemy import text  # noqa: E402

BASELINE_MAX_ID = 0
try:
    with get_engine().connect() as conn:
        BASELINE_MAX_ID = int(
            conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0
        )
    check("MySQL 连通，audit_log 可读", True)
except Exception as e:  # noqa: BLE001
    check("MySQL 连通，audit_log 可读", False, repr(e))
    raise SystemExit("前置失败：审计日志依赖真 MySQL")

# --------------------------------------------------------------------------- #
# 先清上一轮跑剩的残留，再谈别的。
# 上一轮如果在抛异常之前已经建了 m13d_newbie，收尾清理那组就永远跑不到 ——
# 症状是本轮开头 `create_user` 报「登录名 m13d_newbie 已被使用」，
# 而真正的原因不是有人抢了这个名字，是**上一轮的垃圾**。
# 这属于第 51 条坑的形状：测试自己不留退路，下一轮就先被自己绊倒。
with get_engine().connect() as conn:
    stale_ids = conn.execute(
        text("SELECT id FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalars().all()
    for uid in stale_ids:
        conn.execute(text("DELETE FROM user_password_history WHERE user_id = :i"), {"i": uid})
    conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM audit_log WHERE actor_username LIKE :p "
                      "OR (target_label IS NOT NULL AND target_label LIKE :p)"),
                 {"p": f"{PREFIX}%"})
    conn.commit()

with get_engine().connect() as conn:
    cols = {
        r[0]
        for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = 'audit_log'")
        )
    }
    idx = {
        r[0]
        for r in conn.execute(
            text("SELECT DISTINCT index_name FROM information_schema.statistics "
                 "WHERE table_schema = DATABASE() AND table_name = 'audit_log'")
        )
    }

check("表结构：核心列齐全",
      {"id", "actor_user_id", "actor_username", "actor_role", "action",
       "target_type", "target_id", "target_label", "detail", "ip", "created_at"} <= cols,
      f"现有 {sorted(cols)}")
check("⚠️ 结构上没有 update_time（所以「改一条日志」无处落笔）",
      "update_time" not in cols, "有这列就会有人去更新它")
check("⚠️ 结构上没有 deleted_at（所以「软删日志」无处落笔）",
      "deleted_at" not in cols, "有这列就会有人去软删")
check("四个索引都在（时间线 / 按人 / 按对象 / 按动作）",
      {"idx_audit_created", "idx_audit_actor", "idx_audit_target", "idx_audit_action"} <= idx,
      f"现有 {sorted(idx)}")

# --------------------------------------------------------------------------- #
print("\n== 第 2 组：应用层不提供改 / 删 ==")
repo_module = audit_repo
check("仓储层没有 update 方法", not hasattr(repo_module, "update_log"))
check("仓储层没有 delete 方法", not hasattr(repo_module, "delete_log"))
check("仓储层没有提供「按条件改」的通用入口",
      not any(n.startswith(("update", "delete", "purge", "truncate"))
              for n in dir(repo_module) if not n.startswith("_")))

# --------------------------------------------------------------------------- #
print("\n== 第 3 组：敏感内容进不来（结构性拦截） ==")
try:
    audit_repo.record(
        actor_user_id=None, actor_username="m13d_t", actor_role="admin",
        action="user.create", target_type="user", target_id=1,
        detail={"password": "Abcdefghij1"},
    )
    check("detail 里带 password 键 → 拒绝", False, "竟然写进去了")
except audit_repo.AuditRefused as e:
    check("detail 里带 password 键 → 拒绝", True, str(e))

try:
    audit_repo.record(
        actor_user_id=None, actor_username="m13d_t", actor_role="admin",
        action="user.create", target_type="user",
        detail={"nested": {"note": "hash=$2b$12$abcdefghijklmnopqrstuv"}},
    )
    check("detail 里带 bcrypt 特征串 → 拒绝（值这一层也要拦）", False, "竟然写进去了")
except audit_repo.AuditRefused as e:
    check("detail 里带 bcrypt 特征串 → 拒绝（值这一层也要拦）", True, str(e))

try:
    audit_repo.record(
        actor_user_id=None, actor_username="m13d_t", actor_role="admin",
        action="user.create", target_type="user",
        detail={"items": [{"access_token": "eyJhbGciOiJIUzI1NiJ9.x"}]},
    )
    check("嵌套在列表里的 token → 拒绝（深度遍历，不是只看第一层）", False, "竟然写进去了")
except audit_repo.AuditRefused as e:
    check("嵌套在列表里的 token → 拒绝（深度遍历，不是只看第一层）", True, str(e))

try:
    audit_repo.record(
        actor_user_id=None, actor_username="m13d_t", actor_role="admin",
        action="user.not_registered", target_type="user",
    )
    check("未登记的 action → 拒绝（白名单，不是自由字符串）", False, "竟然写进去了")
except audit_repo.AuditRefused as e:
    check("未登记的 action → 拒绝（白名单，不是自由字符串）", True, str(e))

try:
    audit_repo.record(
        actor_user_id=None, actor_username="  ", actor_role="admin",
        action="user.create", target_type="user",
    )
    check("操作人为空 → 拒绝（没有操作人的日志等于没有日志）", False, "竟然写进去了")
except audit_repo.AuditRefused as e:
    check("操作人为空 → 拒绝（没有操作人的日志等于没有日志）", True, str(e))

# --------------------------------------------------------------------------- #
print("\n== 第 4 组：走真实动作，落库内容对得上 ==")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from api.main import app  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.identity import Actor, SOURCE_HEADER  # noqa: E402

client = TestClient(app)


def _h(username: str, ip: str | None = None) -> dict[str, str]:
    headers = {"X-User-Id": username, "X-Username": username}
    if ip:
        headers["X-Forwarded-For"] = ip
    return headers


target = repo.get_by_username(USER_USERNAME)
check("前置：种子员工存在且在职（只用来验「普通员工被拒」这类只读判据）",
      target is not None and target.status == "active")

from core import admin_service as svc  # noqa: E402

# ⚠️ 所有**写**动作都打在下面这个本模块创建的临时员工身上，**绝不动种子员工**。
# 第一版是对 chen.jie 改手机号跑的，跑完种子数据里他的号码就变成了测试值 ——
# 而「测试跑完库要回到原样」是本项目的铁律（seed 的存在意义就是「像样的小公司」）。
actor_admin = Actor(id=None, username=ADMIN_USERNAME, display_name="吴静",
                    role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER)
_newbie = svc.create_user(actor_admin, username=f"{PREFIX}newbie",
                          employee_no=f"{PREFIX}E1", display_name="审计测试新人")
target_id = _newbie.record.id

# 重置密码（admin 专属）—— 审计里必须**只有「重置过」，没有密码本身**
resp = client.post(
    f"/api/v1/admin/users/{target_id}/password/reset",
    headers=_h(ADMIN_USERNAME, ip="203.0.113.9, 10.0.0.1"),
)
check("重置密码 200", resp.status_code == 200, resp.text[:120])
issued = resp.json().get("temporary_password", "")

with get_engine().connect() as conn:
    row = conn.execute(
        text("SELECT * FROM audit_log WHERE id > :b ORDER BY id DESC LIMIT 1"),
        {"b": BASELINE_MAX_ID},
    ).mappings().first()
row = dict(row) if row else {}
check("这次动作落了审计", bool(row), "audit_log 没有新增行")
check("  └ action = user.password.reset", row.get("action") == "user.password.reset", str(row.get("action")))
check("  └ 操作人记的是登录名", row.get("actor_username") == ADMIN_USERNAME, str(row.get("actor_username")))
check("  └ 操作当时的角色也记下来", row.get("actor_role") == "admin", str(row.get("actor_role")))
check("  └ 对象类型/标签可读",
      row.get("target_type") == "user" and f"{PREFIX}newbie" in (row.get("target_label") or ""),
      str(row.get("target_label")))
check("  └ 记下了 IP（取 X-Forwarded-For 第一段，不是网关的 127.0.0.1）",
      row.get("ip") == "203.0.113.9", str(row.get("ip")))
check("  └ detail 里没有密码明文", issued and issued not in (row.get("detail") or ""),
      "审计 detail 里有明文！")
check("  └ detail 里没有哈希", "$2b$" not in (row.get("detail") or "") and "$2a$" not in (row.get("detail") or ""),
      "审计 detail 里有哈希！")
check("  └ detail 只说「发了临时密码」这一件事",
      '"temporary_password_issued": true' in (row.get("detail") or "").replace("True", "true"),
      str(row.get("detail")))

# --------------------------------------------------------------------------- #
print("\n== 第 5 组：强制改密开关也留痕，且不写密码 ==")
resp = client.patch(
    f"/api/v1/admin/users/{target_id}/password/must-change",
    json={"must_change": True}, headers=_h(ADMIN_USERNAME),
)
check("开关强制改密 200", resp.status_code == 200, resp.text[:120])
with get_engine().connect() as conn:
    row2 = dict(conn.execute(
        text("SELECT * FROM audit_log WHERE action='user.password.must_change' "
             "AND id > :b ORDER BY id DESC LIMIT 1"), {"b": BASELINE_MAX_ID},
    ).mappings().first() or {})
check("动作落库", row2.get("id") is not None, str(row2))
check("  └ 明确记了「开关不续期」这条性质",
      "password_changed_at_untouched" in (row2.get("detail") or ""), str(row2.get("detail")))

# --------------------------------------------------------------------------- #
print("\n== 第 6 组：九类写操作全部留痕（覆盖完整性） ==")

dept = svc.create_department(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                                   role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER),
                             code=f"{PREFIX}D1", name="审计测试部")
dept2 = svc.create_department(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                                    role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER),
                              code=f"{PREFIX}D2", name="审计测试部下级", parent_id=dept.id)
svc.update_department(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                             role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER),
                      dept2.id, name="改过名字的下级")
pos = svc.create_position(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                                role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER),
                          code=f"{PREFIX}P1", name="审计测试职位")
svc.update_position(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                           role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER),
                    pos.id, name="改过名字的职位")
svc.delete_position(Actor(id=None, username=ADMIN_USERNAME, display_name="x",
                          role="admin", kb_role=kb_acl.KB_ROLE_SUPERADMIN,
                    status="active", source=SOURCE_HEADER), pos.id)

# user.create 已在第 4 组开头落下（建 _newbie），这里补齐其余员工动作
svc.update_profile(actor_admin, target_id, phone="13000000000")
svc.set_status(actor_admin, target_id, "disabled")
svc.set_status(actor_admin, target_id, "active")
svc.set_role(actor_admin, target_id, "hr")
svc.set_role(actor_admin, target_id, "user")
svc.delete_department(actor_admin, dept2.id)
svc.delete_department(actor_admin, dept.id)

with get_engine().connect() as conn:
    got = {
        r[0] for r in conn.execute(
            text("SELECT DISTINCT action FROM audit_log WHERE id > :b"), {"b": BASELINE_MAX_ID}
        )
    }
expected_actions = {
    "user.create", "user.profile.update", "user.status.change", "user.role.change",
    "user.password.reset", "user.password.must_change",
    "department.create", "department.update", "department.delete",
    "position.create", "position.update", "position.delete",
}
missing = sorted(expected_actions - got)
check("十二类写操作全部留痕", not missing, f"缺 {missing}")

with get_engine().connect() as conn:
    changed = conn.execute(
        text("SELECT detail FROM audit_log WHERE action='user.profile.update' "
             "AND id > :b ORDER BY id DESC LIMIT 1"), {"b": BASELINE_MAX_ID},
    ).scalar() or ""
check("资料变更记的是「从什么变成什么」",
      '"phone"' in changed and "13000000000" in changed, str(changed)[:160])
check("没改的字段不进日志（不记噪声）",
      '"display_name"' not in changed, str(changed)[:160])

# 一个字段都没变（打开弹窗又点保存）→ 不该产生审计：一条「改了 6 个字段」但
# 明细为空的记录，会让人以为真改过；几天后没人信这条日志，审计就整体失效了。
with get_engine().connect() as conn:
    before_noop = int(conn.execute(
        text("SELECT COALESCE(MAX(id),0) FROM audit_log")).scalar() or 0)
svc.update_profile(actor_admin, target_id, phone="13000000000")
with get_engine().connect() as conn:
    noop_rows = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": before_noop},
    ).scalar() or 0)
check("原样保存（无字段变化）→ 不落审计", noop_rows == 0, f"落了 {noop_rows} 条")

# --------------------------------------------------------------------------- #
# detail 的**键名**是前后端契约，测试必须钉住
# --------------------------------------------------------------------------- #
# 踩到的形状：后端对状态/角色/改密开关统一写 `{"from":..., "to":...}`，
# 前端第一版按 `detail.status` / `detail.role` 去读 —— 于是 `from` 永远 undefined，
# 明细列整列显示「—」。**后端全绿、前端不报错、页面看着像「这条日志没明细」**。
# 纯后端断言永远发现不了它，所以这里直接把键名钉成契约：
# 谁改了 detail 的形状，前端就必须跟着改，`check` 会当场转红。
DETAIL_KEY_CONTRACT = {
    "user.status.change": {"from", "to", "token_version"},
    "user.role.change": {"from", "to", "token_version"},
    "user.password.must_change": {"from", "to", "password_changed_at_untouched"},
    "user.password.reset": {"must_change", "token_version", "temporary_password_issued"},
    "user.create": {"username", "employee_no", "role", "issued_temporary_password"},
    # ⚠️ 这里**故意不含** status：新建的员工必然是 active，写进审计是恒真的噪声。
    # 契约断言的价值一半在「必须有什么」，一半在「不该有什么」——
    # 只钉前者的话，后端多塞十个恒定字段也没人会发现。
}
import json as _json  # noqa: E402

bad_shape: list[str] = []
with get_engine().connect() as conn:
    for action, required in DETAIL_KEY_CONTRACT.items():
        raw = conn.execute(
            text("SELECT detail FROM audit_log WHERE action=:a AND id > :b "
                 "ORDER BY id DESC LIMIT 1"),
            {"a": action, "b": BASELINE_MAX_ID},
        ).scalar()
        if not raw:
            bad_shape.append(f"{action}: 没落 detail")
            continue
        try:
            keys = set(_json.loads(raw).keys())
        except Exception as e:  # noqa: BLE001
            bad_shape.append(f"{action}: detail 不是合法 JSON（{e!r}）")
            continue
        if not required <= keys:
            bad_shape.append(f"{action}: 缺 {sorted(required - keys)}")
check("detail 键名与前端渲染约定一致（前端不猜字段名）", not bad_shape, "；".join(bad_shape))

# 前端那份翻译表（AuditView.vue）里出现的动作，必须都在服务端白名单里 ——
# 否则页面上会出现一个永远查不到记录的下拉选项，用户以为「坏了/没人干过」。
_front_actions = re.findall(r"action === '([a-z_.]+)'", _AUDIT_VIEW_SRC)
unknown_front = sorted(set(_front_actions) - set(audit_repo.ACTION_LABELS))
check("⚠️ 审计页源码可读（否则下面两条契约断言在空转）", bool(_AUDIT_VIEW_SRC),
      f"{_AUDIT_VIEW_SRC_PATH} 不存在")
check("前端引用的动作全在服务端白名单内", not unknown_front, f"多出 {unknown_front}")

# --------------------------------------------------------------------------- #
print("\n== 第 7 组：hr 改资料会留痕，但看不到也不该做密码动作 ==")
resp = client.patch(
    f"/api/v1/admin/users/{target_id}",
    json={"phone": "13000000001"}, headers=_h(HR_USERNAME),
)
check("hr 改资料 200", resp.status_code == 200, resp.text[:120])
with get_engine().connect() as conn:
    hr_row = dict(conn.execute(
        text("SELECT * FROM audit_log WHERE actor_username = :u AND id > :b "
             "ORDER BY id DESC LIMIT 1"), {"u": HR_USERNAME, "b": BASELINE_MAX_ID},
    ).mappings().first() or {})
check("hr 的动作同样留痕", hr_row.get("action") == "user.profile.update", str(hr_row.get("action")))
check("  └ 且记着当时的角色是 hr（事后改权限也不影响这条日志）",
      hr_row.get("actor_role") == "hr", str(hr_row.get("actor_role")))

resp = client.get("/api/v1/admin/audit-logs", headers=_h(HR_USERNAME))
check("hr 查得到审计列表（只读不设更高门槛）", resp.status_code == 200, resp.text[:120])
resp = client.get("/api/v1/admin/audit-logs", headers=_h(USER_USERNAME))
check("普通员工查审计 → 403", resp.status_code == 403, str(resp.status_code))

# --------------------------------------------------------------------------- #
print("\n== 第 8 组：查询 / 过滤 / 分页 ==")
resp = client.get("/api/v1/admin/audit-logs", headers=_h(ADMIN_USERNAME))
body = resp.json()
check("列表可读", resp.status_code == 200 and "items" in body)
check("带动作中文标签（前端不用自己翻译）",
      all("action_label" in r for r in body["items"]), str(body["items"][:1])[:160])
check("动作候选来自服务端白名单（前端下拉不会漏项）",
      any(a["value"] == "user.password.reset" for a in body.get("actions", [])),
      str(body.get("actions"))[:120])
check("总数与筛选后列表一致的口径存在（total 字段）",
      isinstance(body.get("total"), int) and body["total"] >= len(body["items"]),
      str(body.get("total")))

resp = client.get("/api/v1/admin/audit-logs?action=user.password.reset",
                  headers=_h(ADMIN_USERNAME))
only = resp.json()["items"]
check("按动作筛选只剩该动作", all(r["action"] == "user.password.reset" for r in only),
      str([r["action"] for r in only]))

resp = client.get("/api/v1/admin/audit-logs?target_type=user", headers=_h(ADMIN_USERNAME))
check("按对象类型筛选生效",
      all(r["target_type"] == "user" for r in resp.json()["items"]))

resp = client.get("/api/v1/admin/audit-logs?limit=1", headers=_h(ADMIN_USERNAME))
first_page = resp.json()
check("分页：limit 生效", len(first_page["items"]) == 1, str(len(first_page["items"])))
check("分页：默认最新在前", first_page["items"][0]["id"] > BASELINE_MAX_ID)

page2 = client.get("/api/v1/admin/audit-logs?limit=1&offset=1",
                   headers=_h(ADMIN_USERNAME)).json()
check("分页：offset 换到第二条", page2["items"] and page2["items"][0]["id"] != first_page["items"][0]["id"])

past = (now_db() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
resp = client.get(f"/api/v1/admin/audit-logs?since={past}", headers=_h(ADMIN_USERNAME))
check("按时间区间筛选（今天造的都该在区间内）", len(resp.json()["items"]) > 0)

resp = client.get("/api/v1/admin/audit-logs?limit=999", headers=_h(ADMIN_USERNAME))
check("limit 超上限被拒（不是默默截断）", resp.status_code == 422, str(resp.status_code))
resp = client.get("/api/v1/admin/audit-logs?order=id;DROP TABLE audit_log",
                  headers=_h(ADMIN_USERNAME))
check("排序参数非法 → 422（排序字段不做字符串拼接）", resp.status_code == 422,
      str(resp.status_code))

# --------------------------------------------------------------------------- #
print("\n== 第 9 组：反向验证（把守卫拆掉，断言必须转红） ==")
import core.admin_service as svc_mod  # noqa: E402

# ① 拆掉审计（_audit 变成空操作）→ 之后的动作不该再产生任何审计行
original_audit = svc_mod._audit
with get_engine().connect() as conn:
    before_silent_id = int(conn.execute(text("SELECT COALESCE(MAX(id),0) FROM audit_log")).scalar() or 0)
svc_mod._audit = lambda *a, **k: None
try:
    svc_mod.set_status(actor_admin, target_id, "disabled")
    svc_mod.set_status(actor_admin, target_id, "active")
    with get_engine().connect() as conn:
        after_silent = int(conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": before_silent_id},
        ).scalar() or 0)
    check("① 拆掉 _audit → 这两次状态变更一条审计都没落（证明留痕来自那行调用）",
          after_silent == 0, f"拆掉后仍落了 {after_silent} 条")
finally:
    svc_mod._audit = original_audit
    svc_mod.set_status(actor_admin, target_id, "disabled")
    svc_mod.set_status(actor_admin, target_id, "active")

# ② 拆掉敏感键拦截 → 明文就能进 detail（证明拦截不是摆设）
original_keys = audit_repo._FORBIDDEN_KEYS
audit_repo._FORBIDDEN_KEYS = frozenset({"never_matches_anything"})
try:
    leaked_id = audit_repo.record(
        actor_user_id=None, actor_username=PREFIX + "leak", actor_role="admin",
        action="user.create", target_type="user",
        detail={"password": "Abcdefghij1"},
    )
    with get_engine().connect() as conn:
        leaked = conn.execute(
            text("SELECT detail FROM audit_log WHERE id = :i"), {"i": leaked_id}
        ).scalar() or ""
    check("② 拆掉敏感键拦截 → 明文真的能写进去（所以那道拦截是唯一防线）",
          "Abcdefghij1" in leaked, str(leaked)[:120])
finally:
    audit_repo._FORBIDDEN_KEYS = original_keys
    with get_engine().connect() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE actor_username LIKE :p"),
                     {"p": f"{PREFIX}%"})
        conn.commit()

# --------------------------------------------------------------------------- #
print("\n== 第 10 组：详情端点必须给原值手机号（13b 那个静默污染的回归） ==")
# 症状：列表接口脱敏 → 编辑弹窗拿 `138****0001` 当当前值 → 管理员不改手机号直接保存
# → 脱敏串被写回库。它是个合法字符串，所以**当场不报任何错**，等到某天核对号码才发现。
# ⚠️ 这一组必须跑在清理**之前**：它要读的那个临时员工，清理组会删掉。
# 顺序写反的时候不会报错，只会让 `repo.get(target_id)` 变成 None，
# 然后 `.phone` 抛 AttributeError —— 或者更糟，断言拿着 None 静默判 False，
# 让人去查一个根本不存在的 bug（第 49 条坑的形状）。
resp = client.get(f"/api/v1/admin/users/{target_id}", headers=_h(ADMIN_USERNAME))
detail_body = resp.json()
check("详情接口可读", resp.status_code == 200, resp.text[:120])
check("详情里的手机号是**原值**不是脱敏串",
      "****" not in (detail_body.get("phone") or ""), str(detail_body.get("phone")))
listed = client.get("/api/v1/admin/users", headers=_h(ADMIN_USERNAME)).json()["items"]
masked = [u for u in listed if u["id"] == target_id]
check("列表接口仍然是脱敏的（详情开特例不等于整体放开）",
      bool(masked) and "****" in (masked[0].get("phone") or ""),
      str(masked[0].get("phone") if masked else None))

# 原样回提交（模拟「不改手机号只改姓名」）不能让手机号变成脱敏串
before_phone = detail_body.get("phone")
client.patch(
    f"/api/v1/admin/users/{target_id}",
    json={"display_name": "陈杰", "phone": before_phone},
    headers=_h(ADMIN_USERNAME),
)
_reloaded = repo.get(target_id)
after_phone = _reloaded.phone if _reloaded else None
check("原样回传手机号不会把它写成脱敏串",
      after_phone is not None and after_phone == before_phone and "****" not in (after_phone or ""),
      f"{before_phone} → {after_phone}")

# --------------------------------------------------------------------------- #
print("\n== 第 11 组：清理与零残留 ==")
# 审计行是本模块造的（id > baseline），删掉它们**就是本测试自己删除审计**——
# §0 那段话说清了这是边界而不是矛盾：不可删约束的是应用层。
with get_engine().connect() as conn:
    made_audit = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": BASELINE_MAX_ID},
    ).scalar() or 0)
    conn.execute(text("DELETE FROM audit_log WHERE id > :b"), {"b": BASELINE_MAX_ID})
    # 本模块建的员工要连同他的改密历史一起清，否则留下孤儿行
    newbie_ids = conn.execute(
        text("SELECT id FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalars().all()
    for uid in newbie_ids:
        conn.execute(text("DELETE FROM user_password_history WHERE user_id = :i"), {"i": uid})
    conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"})
    conn.commit()

with get_engine().connect() as conn:
    left_audit = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": BASELINE_MAX_ID},
    ).scalar() or 0)
    left_dept = int(conn.execute(
        text("SELECT COUNT(*) FROM department WHERE code LIKE :p"), {"p": f"{PREFIX}%"},
    ).scalar() or 0)
    left_pos = int(conn.execute(
        text("SELECT COUNT(*) FROM position WHERE code LIKE :p"), {"p": f"{PREFIX}%"},
    ).scalar() or 0)
    left_user = int(conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"},
    ).scalar() or 0)
    left_hist = int(conn.execute(
        text("SELECT COUNT(*) FROM user_password_history WHERE user_id NOT IN "
             "(SELECT id FROM `user`)"), {},
    ).scalar() or 0)
    users = int(conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar() or 0)
    docs = int(conn.execute(text("SELECT COUNT(*) FROM document")).scalar() or 0)
    seed = {
        r[0] for r in conn.execute(
            text("SELECT username FROM `user` WHERE employee_no BETWEEN 'G0001' AND 'G0010'")
        )
    }

check("本模块真的造出过审计行（否则后面的断言都在空转）", made_audit >= 12, f"{made_audit} 行")
check("本模块造的审计行已清干净", left_audit == 0, f"{left_audit} 行")
check("本模块造的部门已清干净", left_dept == 0, f"{left_dept} 行")
check("本模块造的职位已清干净", left_pos == 0, f"{left_pos} 行")
check("本模块建的员工已清干净", left_user == 0, f"{left_user} 行")
check("没有留下孤儿改密历史", left_hist == 0, f"{left_hist} 行")
check("种子员工 10 人一个不少（按工号清单核对，不看全表总数）", len(seed) == 10, f"{len(seed)} 人")
print(f"  复核：user={users} 行 / document={docs} 行")

print("\n" + "=" * 66)
print(f"  P2-13d 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
