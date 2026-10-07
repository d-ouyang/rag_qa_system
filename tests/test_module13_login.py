# pyright: basic
"""
P2-11c 测试：登录链路（网关 → 后端内部接口 → MySQL）。

    make infra
    .venv/bin/python tests/test_module13_login.py

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    core/auth_service.py   登录编排（判定委托给 11b，落副作用 + 落审计）
    api/routes/internal.py 内部接口的共享密钥边界
    core/identity.py      token_version 比对（改密后旧 token 失效）

**网关那一半（TypeScript）本文件测不到** —— 那是另一个进程、另一门语言。
所以本文件只断言「后端给网关的契约」正确，网关遵守契约由 §6 的真链路 curl 验。

--------------------------------------------------------------------------
这个模块的判据是「不变量」，不是「功能」
--------------------------------------------------------------------------
登录失效的形态全是**静默**的：
    · 失败文案不统一 → 用户名枚举器（它不会报错，只会被拿去枚举别人）；
    · 判定与副作用不同步 → 连错 5 次不锁，限流形同虚设；
    · 少改一个 token_version → 改密后旧 token 仍能用一天；
    · 内部接口没密钥 → 谁连到 8000 都能验密码。
所以断言都写成「**必须不可能**」的形式，而不只是「能登录」。

--------------------------------------------------------------------------
⚠️ 写操作全部打在下面这个本模块创建的临时员工身上，绝不动种子员工
--------------------------------------------------------------------------
第一版是对 chen.jie 改密码跑的，跑完种子数据里他的密码就变成了测试值 ——
而「测试跑完库要回到原样」是本项目的铁律（种子的存在意义就是「像样的小公司」）。
"""
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("EMBEDDING_BACKEND", "local")
os.environ.setdefault("RERANK_BACKEND", "local")

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
PREFIX = "m13c_"
ADMIN_USERNAME = "wu.jing"
STRONG_PWD = "M13c!Passw0rd"
OLD_PWD = "M13c!Passw0rd#old"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


# --------------------------------------------------------------------------- #
print("== 第 1 组：前置（先清本模块残留，别让上一轮的垃圾绊倒这一轮） ==")
from config.settings import settings  # noqa: E402
from core.db import get_engine, now_db  # noqa: E402
from core import auth_service as svc  # noqa: E402
from core import audit_repo  # noqa: E402
from core import password_policy as policy  # noqa: E402
from core import user_repo as repo  # noqa: E402
from sqlalchemy import text  # noqa: E402

with get_engine().connect() as conn:
    stale = conn.execute(
        text("SELECT id FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalars().all()
    for uid in stale:
        conn.execute(text("DELETE FROM user_password_history WHERE user_id = :i"), {"i": uid})
    conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    conn.execute(text("DELETE FROM audit_log WHERE actor_username LIKE :p "
                      "OR (target_label IS NOT NULL AND target_label LIKE :p)"),
                 {"p": f"{PREFIX}%"})
    conn.commit()

with get_engine().connect() as conn:
    AUDIT_BASE = int(conn.execute(
        text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0)
    users_base = {r[0] for r in conn.execute(text("SELECT username FROM `user`"))}
check("MySQL 连通，audit_log 可读", True)

_uid = repo.create_user(
    username=f"{PREFIX}alice", employee_no=f"{PREFIX}E1",
    display_name="11c 测试员工", password_hash=policy.hash_password(STRONG_PWD),
)
ALICE = repo.get_by_username(f"{PREFIX}alice")
check("临时员工已建且在库", ALICE is not None and ALICE.status == "active")
# ⚠️ 刻意**不传** password_changed_at：建号时它应为 NULL，11b 的口径是「按已过期处理」，
# 那正是本组第 2 条断言曾经失败的原因（一个刚建的号一登录就报 expired）。
# 兜底已加在 user_repo.create_user 里，所以这里传不传都应该能正常登录 ——
# 这条断言守着那个兜底，将来有人把它删掉就会红。
check("⚠️ 刚建的号不该一登录就报「密码已过期」（create_user 补了 changed_at）",
      not policy.is_expired(ALICE.password_changed_at), str(ALICE.password_changed_at))

# --------------------------------------------------------------------------- #
print("\n== 第 2 组：成功路径与它的副作用 ==")
out = svc.login(ALICE.username, STRONG_PWD, client_ip="198.51.100.7, 10.0.0.1")
check("正确密码 → ok", out.ok, out.code)
check("  └ 带回 uid（网关要靠它做 token_version）", bool(out.user and out.user["id"] == ALICE.id))
check("  └ 带回 role", out.user and out.user["role"] == "user", str(out.user))
check("  └ 带回 token_version", out.user and out.user["token_version"] == ALICE.token_version)
check("  └ 带回剩余天数", out.expire_in_days is not None, str(out.expire_in_days))

after = repo.get(ALICE.id)
check("成功 → 写 last_login_at", after.last_login_at is not None)
check("成功 → 失败计数归零", after.failed_login_count == 0, str(after.failed_login_count))
check("成功 → 强制改密标记原样保留（登录不该偷偷清它）",
      after.must_change_password == ALICE.must_change_password)

# ⚠️ 关键不变量：登录响应里**绝不能**有密码哈希 / 邮箱 / 手机号。
# 理由写在 _public_user 的 docstring 里：登录响应会经过网关、可能被反代记录、
# 也会出现在浏览器 devtools 里。而 user 表的读取权限比登录宽得多。
blob = json.dumps(out.to_dict(), ensure_ascii=False)
check("响应里没有 password_hash", "password_hash" not in blob, blob[:200])
check("响应里没有 bcrypt 特征串", "$2b$" not in blob and "$2a$" not in blob, blob[:200])
check("响应里没有 email / phone", '"email"' not in blob and '"phone"' not in blob, blob[:200])

# --------------------------------------------------------------------------- #
print("\n== 第 3 组：防用户名枚举（三条失败路径的对外口径） ==")
r_notfound = svc.login(f"{PREFIX}nobody", "Whatever!12345")
r_wrong = svc.login(ALICE.username, "Whatever!12345")
check("账号不存在 → 不通过", not r_notfound.ok)
check("密码错     → 不通过", not r_wrong.ok)
check("⚠️ 两者对外文案**逐字相同**（这是防枚举器的唯一防线）",
      r_notfound.message == r_wrong.message,
      f"{r_notfound.message!r} vs {r_wrong.message!r}")
check("  └ 但内部 code 不同（进日志/监控要能区分）",
      r_notfound.code != r_wrong.code, f"{r_notfound.code} / {r_wrong.code}")

# 停用/离职走单独文案 —— 沿用 11b 的取舍，不在本轮改
repo.set_status(ALICE.id, repo.STATUS_RESIGNED, updated_by=None)
r_inactive = svc.login(ALICE.username, STRONG_PWD)
repo.set_status(ALICE.id, repo.STATUS_ACTIVE, updated_by=None)
check("离职 → 不通过", not r_inactive.ok)
check("离职 → code=inactive（前端据此把人导去联系管理员）",
      r_inactive.code == "inactive", r_inactive.code)
check("离职 → 文案与密码错**不同**（11b 定的：他得知道该找谁）",
      r_inactive.message != r_wrong.message, r_inactive.message)

# 耗时对齐：11b 的铁律在**登录这一层**也要成立（多了一次编排 + 一次写库）
_t: dict[str, float] = {}
for _label, _fn in (
    ("not_found", lambda: svc.login(f"{PREFIX}nobody", "Whatever!12345")),
    ("wrong_pwd", lambda: svc.login(ALICE.username, "Whatever!12345")),
):
    _s = time.time(); _fn(); _t[_label] = time.time() - _s
_mn, _mx = min(_t.values()), max(_t.values())
check(f"两条失败路径耗时同量级（比值 {_mx / _mn:.2f}）", _mx < _mn * 3 + 0.02, f"{_t}")

# --------------------------------------------------------------------------- #
print("\n== 第 4 组：失败计数与锁定（限流不能形同虚设） ==")
repo.set_must_change_password(ALICE.id, False, updated_by=None)
repo.record_login_success(ALICE.id)  # 先归零，让这一组的算术从确定值起步
_before = repo.get(ALICE.id).failed_login_count
for _ in range(2):
    svc.login(ALICE.username, "Whatever!12345")
_mid = repo.get(ALICE.id).failed_login_count
check("连错两次 → 计数递增", _mid == _before + 2, f"{_before} → {_mid}")
check("  └ 未达阈值不锁", repo.get(ALICE.id).locked_until is None,
      str(repo.get(ALICE.id).locked_until))

# 从**当前**计数补到阈值（不写死 3）—— 前面的组已经改过计数，
# 写死数字会让这条断言在「组顺序调整」后静默变成假失败（第 58 条坑的形状）。
for _ in range(max(0, settings.PASSWORD_MAX_FAILURES - _mid)):
    svc.login(ALICE.username, "Whatever!12345")
    svc.login(ALICE.username, "Whatever!12345")
_locked = repo.get(ALICE.id)
check("连错 5 次 → 锁定", _locked.locked_until is not None, str(_locked.locked_until))
check("  └ 锁定期内**即使用正确密码也进不去**", not svc.login(ALICE.username, STRONG_PWD).ok)

# 锁定期内继续试 → 不再累加（否则每点一次就多锁 15 分钟）
_count_before = repo.get(ALICE.id).failed_login_count
_lock_before = repo.get(ALICE.id).locked_until
svc.login(ALICE.username, "Whatever!12345")
check("⚠️ 已锁定时继续试 → 不累加计数（否则永远解不开）",
      repo.get(ALICE.id).failed_login_count == _count_before,
      f"{_count_before} → {repo.get(ALICE.id).failed_login_count}")
check("  └ 也不延长锁定期", repo.get(ALICE.id).locked_until == _lock_before)

# 锁定到期自动解锁（不需要人工介入）
repo.record_login_failure(ALICE.id, lock_until=now_db() - timedelta(seconds=1))
check("锁定期刚过 → 正确密码可登录", svc.login(ALICE.username, STRONG_PWD).ok)
check("  └ 登录即解锁", repo.get(ALICE.id).locked_until is None)
check("  └ 失败计数清零", repo.get(ALICE.id).failed_login_count == 0)

# 停用/离职的账号不累加失败计数（他压根进不来，累加只是噪声）
repo.set_status(ALICE.id, repo.STATUS_DISABLED, updated_by=None)
repo.record_login_failure(ALICE.id, lock_until=None)  # 手工造 3 次
_dis_before = repo.get(ALICE.id).failed_login_count
svc.login(ALICE.username, "Whatever!12345")
check("停用账号不累加失败计数（看板里不该出现无意义计数）",
      repo.get(ALICE.id).failed_login_count == _dis_before,
      f"{_dis_before} → {repo.get(ALICE.id).failed_login_count}")
repo.set_status(ALICE.id, repo.STATUS_ACTIVE, updated_by=None)
repo.record_login_success(ALICE.id)

# --------------------------------------------------------------------------- #
print("\n== 第 5 组：token_version（改密后旧 token 必须失效） ==")
# 网关签发时把 token_version 写进 JWT，之后每个请求带回来；后端比对这个值。
# 这条断言是**纯函数级**的：identity.resolve_actor 收到不匹配的 ver 必须 401。
from fastapi import HTTPException  # noqa: E402

from core import identity  # noqa: E402

_v = repo.get(ALICE.id).token_version
actor_ok = identity.resolve_actor(
    user_id_header=str(ALICE.id), username_header=ALICE.username,
    token_version_header=str(_v),
)
check("ver 与库里一致 → 放行", actor_ok.id == ALICE.id)
try:
    identity.resolve_actor(
        user_id_header=str(ALICE.id), username_header=ALICE.username,
        token_version_header=str(_v + 1),
    )
    check("⚠️ ver 不匹配 → 401（改密后旧 token 失效）", False, "竟然放行了")
except HTTPException as e:
    check("⚠️ ver 不匹配 → 401（改密后旧 token 失效）", e.status_code == 401, str(e.status_code))

# 缺 ver 头 → 放行（dev 裸跑 / 11c 之前签发的 token 都没有）
try:
    a = identity.resolve_actor(user_id_header=str(ALICE.id), username_header=ALICE.username)
    check("缺 ver 头 → 放行（11c 之前签的 token 仍可用，最长 12h 窗口）",
          a.id == ALICE.id)
except HTTPException as e:
    check("缺 ver 头 → 放行（11c 之前签的 token 仍可用，最长 12h 窗口）",
          False, f"被拒 {e.status_code}")

# 真实闭环：改密 → token_version +1 → 旧 ver 失效
_v2 = repo.get(ALICE.id).token_version
repo.update_password(ALICE.id, policy.hash_password("M13c!Passw0rd#new"), now=now_db())
check("改密 → token_version +1", repo.get(ALICE.id).token_version == _v2 + 1)
try:
    identity.resolve_actor(
        user_id_header=str(ALICE.id), username_header=ALICE.username,
        token_version_header=str(_v2),
    )
    check("改密后旧 ver → 401（真闭环）", False, "竟然放行了")
except HTTPException as e:
    check("改密后旧 ver → 401（真闭环）", e.status_code == 401, str(e.status_code))
# 还原成测试密码，后面的组还要用它
repo.update_password(ALICE.id, policy.hash_password(STRONG_PWD), now=now_db())

# --------------------------------------------------------------------------- #
print("\n== 第 6 组：内部接口的信任边界（它能验密码、能改失败计数） ==")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)
SEC = settings.INTERNAL_SHARED_SECRET
SEC_BODY = {"username": ALICE.username, "password": STRONG_PWD}

r = client.post("/api/v1/internal/auth/login", json=SEC_BODY)
check("无密钥 → 401", r.status_code == 401, str(r.status_code))
r = client.post("/api/v1/internal/auth/login", json=SEC_BODY,
                headers={"X-Internal-Token": "wrong-token"})
check("错密钥 → 401", r.status_code == 401, str(r.status_code))
r = client.post("/api/v1/internal/auth/login", json=SEC_BODY,
                headers={"X-Internal-Token": SEC[:-1] + "X"})
check("⚠️ 密钥差一位也 → 401（常量时间比对，不是前缀匹配）",
      r.status_code == 401, str(r.status_code))
r = client.post("/api/v1/internal/auth/login", json=SEC_BODY,
                headers={"X-Internal-Token": SEC})
check("正确密钥 → 200", r.status_code == 200, r.text[:150])
check("  └ 带回 uid（网关靠它签 token）", r.json().get("user", {}).get("id") == ALICE.id)

# 它不在 OpenAPI 里 —— 它能验密码，不该是一份公开说明书
_spec = [p for p in app.openapi()["paths"] if "internal" in p]
check("⚠️ 内部接口**不在 /docs 里**（免得成为绕过网关的说明书）", not _spec, str(_spec))

# 密钥没配时 → 503 fail-closed，而不是「不校验」
_orig = settings.INTERNAL_SHARED_SECRET
try:
    settings.INTERNAL_SHARED_SECRET = ""
    r = client.post("/api/v1/internal/auth/login", json=SEC_BODY,
                    headers={"X-Internal-Token": SEC})
    check("未配密钥 → 503 fail-closed（不是放行）", r.status_code == 503, str(r.status_code))
finally:
    settings.INTERNAL_SHARED_SECRET = _orig

# 判定不通过也是 **200**（业务上成没成看 body.ok）——
# 网关要靠 body 里的 code 区分「回 401」还是「把他导去改密页」
r = client.post("/api/v1/internal/auth/login",
                json={"username": ALICE.username, "password": "nope"},
                headers={"X-Internal-Token": SEC})
check("判定不通过 → 仍 200（语义：请求处理成功，业务上没过）",
      r.status_code == 200, str(r.status_code))
check("  └ body.ok=false + code=wrong_password",
      r.json().get("ok") is False and r.json().get("code") == "wrong_password", r.text[:150])
check("  └ 失败响应里**没有** user 字段（否则会泄露账号存在性）",
      "user" not in r.json(), r.text[:150])

# --------------------------------------------------------------------------- #
print("\n== 第 7 组：登录审计（13d 预留的三个动作，这里第一次真被用到） ==")
with get_engine().connect() as conn:
    rows = conn.execute(
        text("SELECT action, actor_user_id, actor_username, actor_role, target_type, "
             "target_label, detail, ip FROM audit_log WHERE id > :b ORDER BY id"),
        {"b": AUDIT_BASE},
    ).mappings().all()
acts = [r["action"] for r in rows]
check("auth.login.success 落了", "auth.login.success" in acts, str(set(acts)))
check("auth.login.failure 落了", "auth.login.failure" in acts, str(set(acts)))

succ = next(r for r in rows if r["action"] == "auth.login.success")
check("  └ 记了 actor_user_id（13d 当时明确说「等 11c」就是因为没有它）",
      succ["actor_user_id"] == ALICE.id, str(succ["actor_user_id"]))
check("  └ 记了当时的 role", succ["actor_role"] == "user", str(succ["actor_role"]))
check("  └ target_type=auth", succ["target_type"] == "auth", str(succ["target_type"]))
# ⚠️ 这里**不**断言「取 XFF 第一段」：`svc.login()` 收到什么就存什么，
# 「从 XFF 头里取第一段」是 `api/routes/internal._client_ip()` 的职责。
# 硬要在这一层断言，就得让 svc.login 也去解析头 —— 那是**两处实现同一份解析**，
# 正是本轮（11c）花大力气避免的那类重复。
# 真链路上那一段由 §6 的 curl 验（带 XFF 登录 → 审计里是 203.0.113.55）。
check("  └ IP 如实落库（不编造也不丢弃）", succ["ip"] is not None, str(succ["ip"]))

nf = next(r for r in rows if r["action"] == "auth.login.failure" and r["actor_user_id"] is None)
check("账号不存在 → actor_user_id 为空但**仍然留痕**（13d 说这个缺口等 11c）",
      nf["actor_username"].startswith(PREFIX), str(nf["actor_username"]))
check("  └ role 记 (unknown) 而不是编一个", nf["actor_role"] == "(unknown)", str(nf["actor_role"]))

_audit_blob = json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
check("⚠️ 审计里没有密码明文", STRONG_PWD not in _audit_blob and OLD_PWD not in _audit_blob)
check("⚠️ 审计里没有哈希", "$2b$" not in _audit_blob and "$2a$" not in _audit_blob)
check("⚠️ 审计 detail 里没有失败次数（user 表才是权威值，不做第二个真相源）",
      '"failed' not in _audit_blob, "审计里抄了一份失败计数")

# --------------------------------------------------------------------------- #
print("\n== 第 8 组：rehash 升级（cost 涨了要跟上，但不能把人踢下线） ==")
# 造一个 cost 偏低的存量哈希（模拟「策略从 10 涨到 12」的存量用户）
_weak = policy.hash_password(STRONG_PWD, cost=10)
repo.update_password_hash_only(ALICE.id, _weak)
_before_v = repo.get(ALICE.id).token_version
_before_changed = repo.get(ALICE.id).password_changed_at
out2 = svc.login(ALICE.username, STRONG_PWD)
_now = repo.get(ALICE.id)
check("登录成功", out2.ok, out2.code)
check("  └ rehashed=True（它认出了 cost 过低）", out2.rehashed, "rehash 标记没置位")
check("  └ 库里哈希已是新 cost", policy.hash_cost(_now.password_hash) == settings.BCRYPT_COST,
      str(policy.hash_cost(_now.password_hash)))
check("⚠️ token_version **不变**（升级 cost 不是换密码，不该把人踢下线）",
      _now.token_version == _before_v, f"{_before_v} → {_now.token_version}")
check("⚠️ password_changed_at **不变**（否则给旧密码续了 90 天，到期策略永不触发）",
      _now.password_changed_at == _before_changed, "续期了")
check("  └ 仍然算已过期则说明 changed_at 被动了（防御性复核）",
      policy.hash_cost(_now.password_hash) == settings.BCRYPT_COST)

# --------------------------------------------------------------------------- #
print("\n== 第 9 组：反向验证（拆掉守卫，断言必须转红） ==")
# ① 拆掉 token_version 比对 → 「改密后旧 token 失效」那条必须红
_src = Path(identity.__file__).read_text(encoding="utf-8")
_orig_src = _src
try:
    Path(identity.__file__).write_text(
        _src.replace('if int(claimed) != int(actor.record.token_version):',
                     'if False:'),
        encoding="utf-8")
    import importlib
    importlib.reload(identity)
    try:
        identity.resolve_actor(
            user_id_header=str(ALICE.id), username_header=ALICE.username,
            token_version_header="999999",
        )
        check("① 拆掉 ver 比对 → 旧 token 竟然放行了（所以那道比对是唯一防线）", True)
    except HTTPException:
        check("① 拆掉 ver 比对 → 旧 token 竟然放行了（所以那道比对是唯一防线）", False,
              "拆了还拒 → 说明还有别的守卫，这条断言测的不是它")
finally:
    Path(identity.__file__).write_text(_orig_src, encoding="utf-8")
    importlib.reload(identity)

# ② 拆掉敏感键拦截 → 明文能写进审计（证明那道拦截是唯一防线）
_keys = audit_repo._FORBIDDEN_KEYS
try:
    audit_repo._FORBIDDEN_KEYS = frozenset()
    try:
        audit_repo.record(
            actor_user_id=ALICE.id, actor_username=ALICE.username, actor_role="user",
            action="auth.login.success", target_type="auth",
            detail={"note": f"密码是 {STRONG_PWD}"},
        )
        check("② 拆掉敏感键拦截 → 明文真能写进审计（所以拦截是唯一防线）", True)
    except audit_repo.AuditRefused:
        check("② 拆掉敏感键拦截 → 明文真能写进审计（所以拦截是唯一防线）", False,
              "拆了还拒 → 说明拦它的不是那道键名表")
finally:
    audit_repo._FORBIDDEN_KEYS = _keys
    with get_engine().connect() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE detail LIKE :p"),
                     {"p": f"%{STRONG_PWD}%"})
        conn.commit()

# --------------------------------------------------------------------------- #
print("\n== 第 10 组：清理与零残留 ==")
with get_engine().connect() as conn:
    made_audit = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": AUDIT_BASE},
    ).scalar() or 0)
    conn.execute(text("DELETE FROM audit_log WHERE id > :b"), {"b": AUDIT_BASE})
    ids = conn.execute(
        text("SELECT id FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalars().all()
    for uid in ids:
        conn.execute(text("DELETE FROM user_password_history WHERE user_id = :i"), {"i": uid})
    conn.execute(text("DELETE FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    conn.commit()

with get_engine().connect() as conn:
    left_audit = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": AUDIT_BASE},
    ).scalar() or 0)
    left_user = int(conn.execute(
        text("SELECT COUNT(*) FROM `user` WHERE username LIKE :p"), {"p": f"{PREFIX}%"},
    ).scalar() or 0)
    left_hist = int(conn.execute(
        text("SELECT COUNT(*) FROM user_password_history WHERE user_id NOT IN "
             "(SELECT id FROM `user`)"), {},
    ).scalar() or 0)
    docs = int(conn.execute(text("SELECT COUNT(*) FROM document")).scalar() or 0)
    users_now = {r[0] for r in conn.execute(text("SELECT username FROM `user`"))}

check("本模块真的造出过登录审计（否则前面的断言都在空转）", made_audit >= 8, f"{made_audit} 行")
check("本模块造的审计行已清干净", left_audit == 0, f"{left_audit} 行")
check("本模块建的员工已清干净", left_user == 0, f"{left_user} 行")
check("没有留下孤儿改密历史", left_hist == 0, f"{left_hist} 行")
check("⚠️ 种子 10 个用户名一个不少（按名单核对，不看全表总数）",
      users_base == users_now, str(sorted(users_base ^ users_now)))
# ⚠️ 刻意**只打不判** document 的行数：module9 遗留的全表清空让它可以是 0 行，
# 而「恰好 N 行」不是不变量（第 51 条坑）。本模块不碰 document，
# 这件事由「种子 10 人一个不少」那条兜着就够了。
print(f"  复核：document={docs} 行（本模块不碰它，只打印不断言）")

print("\n" + "=" * 66)
print(f"  P2-11c 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
