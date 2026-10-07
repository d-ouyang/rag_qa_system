# pyright: basic
"""
P2-14f 测试：知识库写权限的下发与收回（后端侧）。

    make infra
    .venv/bin/python tests/test_module12_kb_role.py

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    core/admin_service.py::set_kb_role    编排：门槛 / 合法性 / 幂等 / 只禁降级 / 落审计
    core/audit_repo.py::ACTION_LABELS     `user.kb_role.change` 有中文标签
    api/routes/admin.py                   /options 下发五档字典 + PATCH 端点

--------------------------------------------------------------------------
为什么这些判据不能只靠 14e 的矩阵断言
--------------------------------------------------------------------------
14e 的 `test_module12_write_acl.py` 验的是「**判定**」：`kb_role=A` 时动作 B
放不放行。那是**只读**的、纯函数式的。

本模块验的是另外三件矩阵断言**结构上看不见**的东西：

    ① 谁能下发权限（`require_admin` 而不是 `require_staff`）
       —— 这是「权限的权限」，与 ① 正交：hr 能改资料但不能改权限（D10同款取舍）。
    ② 幂等：值没变不动库。
       —— 不幂等的代价很具体：每次「点一下确认」都 `token_version+1`，
       把对方白踢下线一次。而症状是「他莫名掉线」，与本操作毫无关联。
    ③ 只禁降级不禁升级
       —— 禁了升级会带来一个荒唐后果：唯一想维护知识库的人恰好是管理员，
       于是他必须找同事来授权给他（文档字符串里写了这个理由）。

矩阵断言对这三条**完全无感**：它们一个都不改变 `can_upload/can_delete`
的判定结果，只改变「能不能走到判定那一步」。

--------------------------------------------------------------------------
写操作与还原
--------------------------------------------------------------------------
只改**一个**种子员工的 `kb_role`，测完按原值还原（不是「重置成 none」——
那会把测试前的真实状态改掉）。审计行按 id > 基线清掉，
理由与 module12_admin 相同：审计表恰恰是出事那天要靠它的那张表，
不能被测试数据淹没。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module  # noqa: E402

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  SKIP：MySQL 不可达，模块 14f 未执行。")
    print("  本地起中间件：make infra")
    print("=" * 66)
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
print("  连通正常，开始执行\n")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from api.main import app  # noqa: E402
from core import kb_acl  # noqa: E402

client = TestClient(app)

ADMIN_USERNAME = "wu.jing"     # role=admin
HR_USERNAME = "zhou.yan"       # role=hr
USER_USERNAME = "chen.jie"     # role=user
TARGET_USERNAME = "zhao.min"   # 被改权限的对象（挑一个 role=user 的，
#                              免得测出「admin 之间的互相授权」这种边角）

# --------------------------------------------------------------------------- #
# 记基线：目标员工的 kb_role + token_version，以及审计表水位
# --------------------------------------------------------------------------- #
with db_module.get_engine().connect() as conn:
    AUDIT_BASE = int(conn.execute(
        text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0)


def _row(username: str):
    with db_module.get_engine().connect() as conn:
        r = conn.execute(
            text("SELECT id, username, role, kb_role, token_version, status FROM `user` "
                 "WHERE username = :u"),
            {"u": username},
        ).fetchone()
    return None if r is None else {
        "id": r[0], "username": r[1], "role": r[2],
        "kb_role": r[3], "token_version": r[4], "status": r[5],
    }


ADMIN = _row(ADMIN_USERNAME)
HR = _row(HR_USERNAME)
USER = _row(USER_USERNAME)
TARGET_BASELINE = _row(TARGET_USERNAME)
check("三个角色 + 一个被授权对象都在（种子齐全）",
      all(x is not None for x in (ADMIN, HR, USER, TARGET_BASELINE)),
      f"admin={ADMIN is not None} hr={HR is not None} user={USER is not None} "
      f"target={TARGET_BASELINE is not None}")


def _h(row) -> dict:
    """TestClient 的身份头（后端只认 X-User-Id + 网关证明；dev 模式下证明可省）。"""
    return {"X-User-Id": str(row["id"])}


def _restore() -> None:
    """把目标员工的 kb_role 与 token_version 还原成基线值。

    刻意**按原值还原**而不是「设成 none」—— 测试前的状态可能是别人
    （比如 14f 手工授权过）留下的，一还原成 none 就把人家的状态抹了。
    """
    with db_module.get_engine().begin() as conn:
        conn.execute(
            text("UPDATE `user` SET kb_role = :kb, token_version = :tv WHERE id = :i"),
            {"kb": TARGET_BASELINE["kb_role"], "tv": TARGET_BASELINE["token_version"],
             "i": TARGET_BASELINE["id"]},
        )


# --------------------------------------------------------------------------- #
section("第 1 组：门槛 —— 谁能改知识库写权限")
# --------------------------------------------------------------------------- #
r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={"kb_role": "ops"}, headers=_h(ADMIN))
check("admin 能下发（200/204）", r.status_code in (200, 204),
      f"{r.status_code} {r.text[:160]}")

r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={"kb_role": "none"}, headers=_h(HR))
check("🔴 hr 不能下发（D10 同款：权限变更一律 admin 专属）", r.status_code == 403,
      f"{r.status_code} {r.text[:160]}")

r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={"kb_role": "superadmin"}, headers=_h(USER))
check("🔴 普通员工不能下发", r.status_code == 403, f"{r.status_code} {r.text[:160]}")

# --------------------------------------------------------------------------- #
section("第 2 组：只禁降级，不禁升级（改自己）")
# --------------------------------------------------------------------------- #
r = client.patch(f"/api/v1/admin/users/{ADMIN['id']}/kb-role",
                 json={"kb_role": "superadmin"}, headers=_h(ADMIN))
check("admin 把自己设成 superadmin → 放行（升自己的权无害）",
      r.status_code in (200, 204), f"{r.status_code} {r.text[:200]}")

r = client.patch(f"/api/v1/admin/users/{ADMIN['id']}/kb-role",
                 json={"kb_role": "none"}, headers=_h(ADMIN))
check("🔴 admin 把自己降到 none → 403（否则恢复要找别人）",
      r.status_code == 403, f"{r.status_code} {r.text[:200]}")
check("降级被拒的错误文案说清了后果（不是干巴巴的「不允许」）",
      r.status_code != 403 or ("自己" in r.text and "权限" in r.text),
      r.text[:200])

# --------------------------------------------------------------------------- #
section("第 3 组：幂等 —— 值没变不动库")
# --------------------------------------------------------------------------- #
_restore()
r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={"kb_role": "ops"}, headers=_h(ADMIN))
check("先真改成 ops（建立前提）", r.status_code in (200, 204),
      f"{r.status_code} {r.text[:160]}")
_mid = _row(TARGET_USERNAME)
check("库里的 kb_role 确实是 ops", _mid["kb_role"] == "ops", str(_mid["kb_role"]))

r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={"kb_role": "ops"}, headers=_h(ADMIN))
_after = _row(TARGET_USERNAME)
check("再改成同一个值 → 仍成功（幂等不报错）", r.status_code in (200, 204),
      f"{r.status_code} {r.text[:160]}")
check("🔴 但 token_version 没有 +1（否则把对方白踢下线一次）",
      _after["token_version"] == _mid["token_version"],
      f"改前={_mid['token_version']} 改后={_after['token_version']}")

# --------------------------------------------------------------------------- #
section("第 4 组：非法值是 400 而不是 500")
# --------------------------------------------------------------------------- #
# ⚠️ 这里**刻意**包含带空白 / 大小写变体的档位，并期望它们被 400 拒掉。
#
# `kb_acl` 里有两条入口，宽松程度**故意不同**：
#   · `normalize()`       —— 读路径，strip + lower，未知降级成 none，永不抛。
#     宽松是必需的：14a 实测过手工 SQL 灌进库的 `'Ops '`，不规整就判据失配。
#   · `is_valid_kb_role()` —— 写路径，本模块用的就是它，**只认规范值**。
#
# 为什么写路径不该宽松（这是 14f 修的一个真问题，不是测试吹毛求疵）：
# 写路径的输入来自管理端下拉框，14f 已规定前端不接受自由输入 ——
# 宽松在这条路上没有正当来源，却会「静默改写别人的权限」：
# 管理员发来 `'SUPERADMIN'`、库里存成 `superadmin`，审计 detail 里 from/to
# 记的也是规范化后的值，于是「我明明选了 A，它存成了 B」在任何一界都看不出来。
# 宁可 400 让人重选一次。这与 `set_role` 的 `role in repo.ROLES` 同规格。
for bad in ["superadmin ", " SUPERADMIN", "SUPERADMIN", "superadmin\n",
            "root", "", "Ops", "0"]:
    r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                     json={"kb_role": bad}, headers=_h(ADMIN))
    check(f"非规范档位 {bad!r} → 400（写路径只认规范值，不静默改写）",
          r.status_code == 400, f"{r.status_code} {r.text[:160]}")

# 反向对照：读路径**必须**是宽松的（否则库里一条脏值就让权限失配）。
# 这一条是给「收紧写路径」这个改动立的护栏 —— 有人为了对称把
# `normalize` 也改成严格，就会把 14a 修过的那个洞重新打开，且不报错。
check("反向对照：normalize() 读路径仍然宽松（'Ops ' → 'ops'）",
      kb_acl.normalize("Ops ") == "ops" and kb_acl.normalize(" SUPERADMIN ") == "superadmin",
      f"'Ops '→{kb_acl.normalize('Ops ')!r} ' SUPERADMIN '→{kb_acl.normalize(' SUPERADMIN ')!r}")
check("反向对照：normalize() 对真正的未知值仍然降级成 none（不抛）",
      kb_acl.normalize("root") == "none" and kb_acl.normalize(None) == "none",
      f"'root'→{kb_acl.normalize('root')!r} None→{kb_acl.normalize(None)!r}")
check("反向对照：is_canonical_kb_role() 严格（'Ops ' 不合法）",
      kb_acl.is_canonical_kb_role("ops")
      and not kb_acl.is_canonical_kb_role("Ops ")
      and not kb_acl.is_canonical_kb_role(" SUPERADMIN"),
      f"'ops'={kb_acl.is_canonical_kb_role('ops')} "
      f"'Ops '={kb_acl.is_canonical_kb_role('Ops ')} "
      f"' SUPERADMIN'={kb_acl.is_canonical_kb_role(' SUPERADMIN')}")
check("反向对照：is_valid_kb_role() 仍宽松（仓储层入口，14a 的洞不能重开）",
      kb_acl.is_valid_kb_role("Ops ") and not kb_acl.is_valid_kb_role("root"),
      f"'Ops '={kb_acl.is_valid_kb_role('Ops ')} 'root'={kb_acl.is_valid_kb_role('root')}")

r = client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
                 json={}, headers=_h(ADMIN))
check("缺 kb_role 字段 → 422（FastAPI 参数校验）", r.status_code == 422,
      f"{r.status_code} {r.text[:160]}")

# --------------------------------------------------------------------------- #
section("第 5 组：审计（写权限变更必须留痕，且与 role 变更可区分）")
# --------------------------------------------------------------------------- #
_restore()
before = _row(TARGET_USERNAME)
client.patch(f"/api/v1/admin/users/{TARGET_BASELINE['id']}/kb-role",
             json={"kb_role": "dev"}, headers=_h(ADMIN))
after = _row(TARGET_USERNAME)
check("真变更会 +1 token_version（改权限要踢掉旧 token）",
      after["token_version"] == before["token_version"] + 1,
      f"前={before['token_version']} 后={after['token_version']}")

with db_module.get_engine().connect() as conn:
    rows = conn.execute(
        text("SELECT action, detail FROM audit_log WHERE id > :b ORDER BY id"),
        {"b": AUDIT_BASE},
    ).fetchall()
kb_audits = [(a, d) for a, d in rows if a == "user.kb_role.change"]
check("落了 user.kb_role.change 审计", len(kb_audits) >= 1, f"{len(kb_audits)} 条")

from core import audit_repo  # noqa: E402

check("🔴 动作不复用 user.role.change（审计里要能分清「改后台权限」与「改知识库权限」）",
      "user.kb_role.change" in audit_repo.ACTION_LABELS,
      "ACTION_LABELS 里没有这一项 → 审计列表明细会显示成英文动作名")
check("user.kb_role.change 有中文标签",
      audit_repo.ACTION_LABELS.get("user.kb_role.change") == "变更知识库写权限",
      f"实际={audit_repo.ACTION_LABELS.get('user.kb_role.change')!r}")

if kb_audits:
    import json as _json
    detail = _json.loads(kb_audits[-1][1]) if isinstance(kb_audits[-1][1], str) else kb_audits[-1][1]
    check("审计 detail 记了 from/to 档位",
          isinstance(detail, dict) and detail.get("from") == before["kb_role"]
          and detail.get("to") == "dev",
          f"detail={detail}")
    check("审计 detail 记了档位的中文标签（界面能直接显示，不必再查表）",
          isinstance(detail, dict) and bool(detail.get("to_label")),
          f"detail={detail}")
    check("审计 detail 记了改后的 token_version（排障时能对上「他被踢下线」）",
          isinstance(detail, dict) and detail.get("token_version") == after["token_version"],
          f"detail={detail}库里={after['token_version']}")

# --------------------------------------------------------------------------- #
section("第 6 组：/options 下发五档字典（前端不自己维护一份）")
# --------------------------------------------------------------------------- #
r = client.get("/api/v1/admin/options", headers=_h(ADMIN))
check("/options 可读", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
opts = r.json() if r.status_code == 200 else {}
kbs = opts.get("kb_roles")
check("🔴 /options 下发了 kb_roles 字典（前端标签与 capabilities 全部后端派生）",
      isinstance(kbs, list) and len(kbs) >= 5, f"实际={kbs!r}")

if isinstance(kbs, list) and kbs:
    by_value = {x.get("value"): x for x in kbs}
    check("五档齐全（none/ops/qa/dev/superadmin）",
          set(by_value) >= set(kb_acl.KB_ROLES) - {"unknown"},
          f"实际={sorted(by_value)}")
    check("每档都有中文 label",
          all(x.get("label") for x in kbs),
          str([x.get("value") for x in kbs if not x.get("label")]))
    check("每档都带 capabilities（前端直接照它显示按钮，不重算一遍）",
          all(isinstance(x.get("capabilities"), dict) and x["capabilities"]
              for x in kbs),
          str([x.get("value") for x in kbs if not x.get("capabilities")]))
    # 字典里的 capabilities 必须与 kb_acl 的一致 —— 两处各维护一份就会漂
    mismatch = [
        v for v, x in by_value.items()
        if v in kb_acl.KB_ROLES and x.get("capabilities") != kb_acl.capabilities(v)
    ]
    check("🔴 下发的 capabilities 与 core/kb_acl 逐档一致（不派生第二份真相源）",
          not mismatch, f"不一致的档位={mismatch}")
    check("unknown 不在字典里（它是「值不认识」的形状，不是一档权限）",
          "unknown" not in by_value, f"字典里有 unknown：{by_value.get('unknown')}")
    # ⚠️ `none` 必须**只有** can_read —— 一旦它带上任何写能力，
    # 「默认谁都只能读」这条 14a 的核心裁决就被悄悄改掉了。
    none_cap = by_value.get("none", {}).get("capabilities") or {}
    check("🔴 none 档没有任何写能力（14a「读开放、写收紧」的默认）",
          not none_cap.get("upload") and not none_cap.get("delete")
          and not none_cap.get("reindex"),
          f"none={none_cap}")

# --------------------------------------------------------------------------- #
section("第 7 组：还原 + 零残留")
# --------------------------------------------------------------------------- #
_restore()
now = _row(TARGET_USERNAME)
check("目标员工的 kb_role 已还原成基线值", now["kb_role"] == TARGET_BASELINE["kb_role"],
      f"基线={TARGET_BASELINE['kb_role']} 现在={now['kb_role']}")
check("目标员工的 token_version 已还原成基线值",
      now["token_version"] == TARGET_BASELINE["token_version"],
      f"基线={TARGET_BASELINE['token_version']} 现在={now['token_version']}")

# 审计清理（理由见文件头：审计表不能被测试数据淹没）
made = 0
with db_module.get_engine().begin() as conn:
    made = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"),
        {"b": AUDIT_BASE}).scalar() or 0)
    conn.execute(text("DELETE FROM audit_log WHERE id > :b"), {"b": AUDIT_BASE})
with db_module.get_engine().connect() as conn:
    left = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"),
        {"b": AUDIT_BASE}).scalar() or 0)
# 删之前确认真造过 —— 「清理逻辑坏了（永远删 0 行）」也显示全绿是第 49 条坑的形状
check("本模块确实落了审计（否则清理断言在空转）", made > 0, f"{made} 行")
check("本模块造的审计行已清干净", left == 0, f"{left} 行")

with db_module.get_engine().connect() as conn:
    users_now = {r[0] for r in conn.execute(text("SELECT username FROM `user`"))}
check("⚠️ 种子用户名一个不少（按名单核对，不看全表总数）",
      users_now >= set(module_seed_names := (
          "wu.jing", "zhou.yan", "chen.jie", "zhao.min", "sun.lei",
          "li.na", "zhang.wei", "wang.fang", "xu.hao", "zheng.shuang")),
      str(sorted(users_now - set(module_seed_names))))

print()
print("=" * 66)
print(f"  P2-14f 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)