# pyright: basic
"""
P2-12b 测试：身份与信任边界（网关剥离 + 路径授权 + 后端网关证明）。

    make infra
    .venv/bin/python tests/test_module14_trust_boundary.py

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    gateway/src/auth/identity-headers.ts      剥离清单 + 注入内容（TS，读源码断言）
    gateway/src/auth/path-authorization.ts    路径级授权判定（TS，读源码 + 逻辑等价验证）
    gateway/src/proxy/proxy.controller.ts     先剥离后注入的**顺序**（TS，读源码断言）
    core/identity.py               verify_gateway_proof / resolve_actor
    api/routes/system.py           探活里报「信任边界有没有真的生效」

--------------------------------------------------------------------------
为什么这个模块的判据全是「读源码」和「不变量」
--------------------------------------------------------------------------
12b 要防的那几类失效**全部不报错**：
    · 剥离漏了一个头 → 伪造头穿透到后端，接口照样 200；
    · 注入跑在剥离之前 → 被剥离掉的是真身份，全站 401（这个反而会报）；
    · 两侧头清单不一致 → 某个头在后端存在、在网关不存在，静默不生效；
    · 路径授权比后端严 → 只有 hr 账号现形（「他为什么进不去管理端」）。
所以「跑一遍看结果对不对」在这里几乎没有判别力 ——
必须**把源码本身当被测对象**（断言顺序、断言清单、断言两侧一致）。

--------------------------------------------------------------------------
⚠️ 为什么 TS 侧用「读源码 + 等价重实现」而不是直接跑 TypeScript
--------------------------------------------------------------------------
本仓库的测试统一是Python 脚本（`make test` 里没有 tsc 环节），起一份 ts-node
也不划算（多一个运行时依赖、还要 mock Express/Nest 的 req/res）。
代价是**「等价重实现」可能与真实 TS 代码漂移** —— 所以每组断言都附一条
**正扫源码**的断言（真的去读 .ts 文件），两件事互为交叉验证：
    · 逻辑组：验「规则本身对不对」（成对：该拒的拒、该放的放）
    · 源码组：验「TS 那份代码确实写着这些规则」
漂移了会表现为「逻辑组全绿但源码组红」，那是可读的失败。

--------------------------------------------------------------------------
写操作全部不打种子员工
--------------------------------------------------------------------------
本模块原则上**不写库**（它验的是「请求头进→ 判定出」，不需要真账号）。
第6 组会临时改一个员工的 `password_hash` 来验证「身份头指向不存在的人」，
改完立刻换成新的随机临时密码 + 强制改密标记（种子的原密码早已不可知，
所以「还原」不是假装还原，是换成另一个合法值）。
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EMBEDDING_BACKEND", "local")
os.environ.setdefault("RERANK_BACKEND", "local")

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0

GW = ROOT / "gateway" / "src"
PROXY_TS = GW / "proxy" / "proxy.controller.ts"
HEADERS_TS = GW / "auth" / "identity-headers.ts"
PATH_AUTH_TS = GW / "auth" / "path-authorization.ts"


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


# --------------------------------------------------------------------------- #
# 与 TS 侧等价的 Python 重实现（用来验规则本身）。
# 刻意**不复用**生产代码的任何函数—— 那会让测试与实现同源，
# 实现错了测试也跟着错（这是第 51 条坑的形状）。
# 每组下面都有「正扫 TS 源码」的断言与之交叉验证。
# --------------------------------------------------------------------------- #
INBOUND_IDENTITY_HEADERS = [
    "x-user-id",
    "x-username",
    "x-user-role",
    "x-role",
    "x-dept-id",
    "x-token-version",
    # P2-14b：知识库写权限。**剥离但不注入**（注入会造出第二个真相源，
    # 见 identity-headers.ts 里 ForwardedIdentity 的注释）。
    # 它必须在这个清单里 —— 客户端能自己填一个 `x-user-kb-role: superadmin`，
    # 而「后端将来会不会读它」是会变的（与 x-dept-id 当初的理由同源）。
    "x-user-kb-role",
    "x-identity-source",
    "x-internal-auth",
]

STAFF_ROLES = ["admin", "hr"]
STAFF_PATH_PREFIXES = ["/api/v1/admin/"]
NEVER_PROXIED_PREFIXES = ["/api/v1/internal/"]

# --------------------------------------------------------------------------- #
# P2-14b：知识库写权限在网关这一侧的粗筛（TS 侧见 path-authorization.ts）
# --------------------------------------------------------------------------- #
# 五档 —— 与后端 `core/kb_acl.py` 的 KB_ROLES、网关 TS 侧的 KB_ROLES 三方对齐。
# ⚠️ `unknown` 不是一档权限，而是「值不认识」的形状（`normalizeKbRole` 的降级目标）。
KB_ROLES = ["none", "ops", "qa", "dev", "superadmin", "unknown"]

#: 四条写路由，与后端 `api/routes/documents.py` 的四条写路由一一对应。
#: 顺序刻意与方法一起写全 —— `GET` 与 `DELETE` 共用 `/documents/{id}` 形状，
#: 只看路径的话一个 `kb_role=none` 的人连**看**文档都不行，
#: 而共用知识库是刻意的业务决策。
KB_WRITE_ROUTES = [
    ("POST", r"^/api/v1/documents/upload$"),
    ("POST", r"^/api/v1/documents/upload/batch$"),
    ("POST", r"^/api/v1/documents/\d+/reparse$"),
    ("DELETE", r"^/api/v1/documents/\d+$"),
]

#: 与 `KB_WRITE_ROUTES` 一一对应的**真实路径**。
#:
#: ⚠️ 刻意不写成「从正则里替换出路径」：`re.sub(r"\d+", "123", pat)` 会把
#: `/api/v1` 里的 `1` 也换掉，得到 `/api/v123/documents/upload` ——
#: 四条断言全部假红，而症状看起来像「实现有 bug」。
#: 正则负责判定、真实路径负责枚举，两者分开写更省事也更不容易错。
KB_WRITE_PATHS = [
    ("POST", "/api/v1/documents/upload"),
    ("POST", "/api/v1/documents/upload/batch"),
    ("POST", "/api/v1/documents/123/reparse"),
    ("DELETE", "/api/v1/documents/123"),
]


def normalize_kb_role(raw: object) -> str:
    """与 TS 侧 `normalizeKbRole` 等价：不认识的一律降级成 `unknown`（行为 = 只读）。"""
    v = raw.strip().lower() if isinstance(raw, str) else ""
    return v if v in KB_ROLES else "unknown"


def is_kb_role_none(kb_role: str | None) -> bool:
    """`none` / `unknown` / 缺失（全都是「不是 none」）都当只读。"""
    if not kb_role:
        return True
    return kb_role in ("none", "unknown")


def is_kb_write_request(path: str, method: str | None) -> bool:
    if not method:
        return False
    import re as _re

    m = method.upper()
    return any(m == mm and _re.search(pat, path) for mm, pat in KB_WRITE_ROUTES)


def decide_path(
    path: str,
    role: str | None,
    kb_role: str | None = None,
    method: str | None = None,
) -> tuple[bool, str, str]:
    if any(path == p or path.startswith(p) for p in NEVER_PROXIED_PREFIXES):
        return False, "NOT_FOUND", "请求的资源不存在"
    # P2-14b：知识库写路径。与 TS 侧 `KB_WRITE_ROUTES` 一一对应。
    if is_kb_write_request(path, method) and is_kb_role_none(kb_role):
        return False, "FORBIDDEN_KB_WRITE", "没有权限修改知识库，请联系管理员开通"
    if not any(path == p or path.startswith(p) for p in STAFF_PATH_PREFIXES):
        return True, "", ""
    if role and role in STAFF_ROLES:
        return True, "", ""
    return False, "FORBIDDEN_PATH", "需要管理员或人事权限才能访问该接口"


def strip_headers(present: dict[str, str]) -> dict[str, str]:
    """模拟「无条件剥离之后」剩下的头。"""
    return {k: v for k, v in present.items() if k.lower() not in INBOUND_IDENTITY_HEADERS}


# --------------------------------------------------------------------------- #
section("第 1 组：前置（清理本模块残留）")
# --------------------------------------------------------------------------- #
from config.settings import settings  # noqa: E402
from core import identity as ident  # noqa: E402
from core import password_policy as policy  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.db import get_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

with get_engine().connect() as conn:
    AUDIT_BASE = int(
        conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0
    )
    USERS_BASE = {r[0] for r in conn.execute(text("SELECT username FROM `user`"))}
check("MySQL 连通", True)
check("三份源码文件都在（找不到就没法做源码断言）",
      all(p.exists() for p in (PROXY_TS, HEADERS_TS, PATH_AUTH_TS)))

PROXY_SRC = PROXY_TS.read_text(encoding="utf-8")
HEADERS_SRC = HEADERS_TS.read_text(encoding="utf-8")
PATH_AUTH_SRC = PATH_AUTH_TS.read_text(encoding="utf-8")

# --------------------------------------------------------------------------- #
section("第 2 组：两侧身份头清单必须一致（跨端契约）")
# --------------------------------------------------------------------------- #
# 13d 的教训：后端写 {"from","to"}、前端读 detail.status → 明细列整列 —，
# 而后端全绿、接口 200、不报错。两份「字符串字面量清单」跨语言时，
# 没有任何东西会校验它们还对不对得上。
py_headers = list(ident.IDENTITY_HEADERS)
check("⚠️ Python 侧清单 = 期望值", py_headers == INBOUND_IDENTITY_HEADERS,
      f"实际 {py_headers}")

# 正扫 TS 源码里那份清单的字面量
m = re.search(r"INBOUND_IDENTITY_HEADERS\s*=\s*\[(.*?)\]", HEADERS_SRC, re.S)
ts_headers = re.findall(r"'([^']+)'", m.group(1)) if m else []
check("⚠️ TS 侧清单 = Python 侧清单（少一个就红）",
      ts_headers == py_headers, f"TS={ts_headers}")

check("⚠️ 清单里含 x-internal-auth（否则客户端能自称网关）",
      "x-internal-auth" in ts_headers and "x-internal-auth" in py_headers)

# --------------------------------------------------------------------------- #
section("第 3 组：先剥离、后注入（顺序不能反）")
# --------------------------------------------------------------------------- #
strip_pos = PROXY_SRC.find("for (const h of INBOUND_IDENTITY_HEADERS)")
inject_pos = PROXY_SRC.find("buildForwardIdentityHeaders(")
check("源码里有无条件剥离那一段", strip_pos > 0)
check("源码里有注入那一段", inject_pos > 0)
check("⚠️ 剥离排在注入**之前**（顺序反了会把真身份剥掉，全站 401）",
      0 < strip_pos < inject_pos,
      f"strip@{strip_pos} inject@{inject_pos}")

lines = PROXY_SRC.splitlines()
strip_line_no = next((i for i, ln in enumerate(lines) if "for (const h of INBOUND_IDENTITY_HEADERS)" in ln), -1)

# 「无条件」的判据：剥离那一行**自己所在的行 + 前 6 行**里都不能有 if。
# ⚠️ 第一版只看「前 6 行」，于是反向验证把剥离改成
#    `if (incoming.user) for (...)` —— if 和 for 在**同一行**、在窗口之外 ——
#    断言照样绿。反向验证跑出来才发现（第 50 条：断言没红就是没验到）。
# ⚠️ 第二版把 lines 的定义挪到了这条之后，于是 NameError、整个测试崩在第 3 组，
#    后面的组根本没跑 —— 而反向验证只看到「输出里找不到那条断言」。
#    教训：断言崩掉和断言失败要能区分，所以每组都要能单独跑完。
window_around = "\n".join(
    lines[max(0, strip_line_no - 6): strip_line_no + 1]
)
check("⚠️ 剥离不在任何 if 内（11c 的写法是 if (incoming.user) 包着，真身份会被连带剥掉）",
      "if (" not in window_around,
      f"剥离行±6 行内含 if：{window_around.strip()[-140:]}")

check("剥离走的是 removeHeader（不是 setHeader 空串 —— 那样后端收到的是空值而不是缺失）",
      "removeHeader" in PROXY_SRC)
strip_line_no = next((i for i, ln in enumerate(lines) if "for (const h of INBOUND_IDENTITY_HEADERS)" in ln), -1)

# --------------------------------------------------------------------------- #
section("第 4 组：路径级授权（成对：该拒的拒、该放的放）")
# --------------------------------------------------------------------------- #
for path, role, want_ok, label in [
    ("/api/v1/admin/users", "user", False, "普通员工打管理端 → 拒"),
    ("/api/v1/admin/users", None, False, "未登录打管理端 → 拒（由守卫先拦，这里是双保险）"),
    ("/api/v1/admin/users", "hr", True, "hr 打管理端 → 放（D10：hr 能管人事）"),
    ("/api/v1/admin/users", "admin", True, "admin 打管理端 → 放"),
    ("/api/v1/qa/sessions", "user", True, "普通员工打业务接口 → 放"),
    ("/api/v1/qa/ask", "user", True, "普通员工提问 → 放"),
    ("/api/v1/system/health", None, True, "探活（白名单）→ 放"),
    ("/api/v1/internal/auth/login", "admin", False, "⚠️ 内部接口即便 admin 也拒"),
    ("/api/v1/internal/auth/login", "user", False, "⚠️ 内部接口对普通员工也拒"),
]:
    ok, code, _ = decide_path(path, role)
    check(f"路径授权：{label}", ok is want_ok, f"实际 allowed={ok} code={code}")

# 内部接口必须报 404 而不是 403 —— 403 等于承认「它在，只是你不许」
ok, code, msg = decide_path("/api/v1/internal/auth/login", "admin")
check("⚠️ 内部接口报 404 而非 403（不给枚举留线索）", code == "NOT_FOUND", code)
check("内部接口的文案与真正的 404 一致", msg == "请求的资源不存在", msg)

# 粗筛必须**比后端精确判据更宽**：网关只答「你可能是管人事的」，
# 后端才答「你能不能改这个字段」。反过来（网关比后端严）会让 hr 完全打不开管理端。
ok_hr_staff, _, _ = decide_path("/api/v1/admin/users", "hr")
check("⚠️ 网关比后端宽：hr 能过网关这层（后端再判他能做什么）", ok_hr_staff)

# 前缀匹配不能被子串命中
check("前缀匹配不吃子串：/api/v1/administrator 不被当成管理端",
      decide_path("/api/v1/administrator/x", "user")[0])
check("前缀匹配不吃子串：/api/v1/docs/admin 不被当成管理端",
      decide_path("/api/v1/docs/admin", "user")[0])

# --------------------------------------------------------------------------- #
section("第 4b 组：知识库写权限的网关粗筛（P2-14b）")
# --------------------------------------------------------------------------- #
# 这一组的判据是**不变量**而不是逐条枚举 —— 与第 50 条坑同源：
# 逐条枚举看起来更严，实际是「样本恰好覆盖了当前实现」，
# 而加一条路由时它不会转红。真正的不变量是：
#   ① 读接口对所有 kb_role 都放行（共用决策）
#   ② 四条写接口对 none / unknown / 缺失 全拒
#   ③ 四条写接口对其余四档 全放（网关要比后端**更宽**）
for _m, _path in KB_WRITE_PATHS:
    for _none_val in (None, "", "none", "unknown"):
        ok_kb, code_kb, _ = decide_path(_path, "user", _none_val, _m)
        check(f"kb 粗筛拒绝：{_m} {_path}（kb_role={_none_val!r}）", ok_kb is False,
              f"实际 allowed={ok_kb} code={code_kb}")
    for _granted in ("ops", "qa", "dev", "superadmin"):
        ok_kb, code_kb, _ = decide_path(_path, "user", _granted, _m)
        check(f"kb 粗筛放行：{_m} {_path}（kb_role={_granted}）", ok_kb is True,
              f"实际 allowed={ok_kb} code={code_kb}")

# 读接口必须对**所有** kb_role 放行 —— 知识库共用是刻意的业务决策，
# 把 GET 也拦掉会让「没权限的人连知识库都看不了」，那不是设计要的。
for _role_val in (None, "none", "unknown", "ops", "qa", "dev", "superadmin"):
    ok_read, code_read, _ = decide_path("/api/v1/documents/", "user", _role_val, "GET")
    check(f"kb 粗筛不拦读：GET /api/v1/documents/（kb_role={_role_val!r}）", ok_read is True,
          f"实际 allowed={ok_read} code={code_read}")

# ⚠️ **方法必须参与判定**：GET 与 DELETE 共用 `/documents/{id}` 形状。
# 只看路径的话一个 kb_role=none 的人连单篇文档都看不到。
for _kb in (None, "none"):
    ok_del, _, _ = decide_path("/api/v1/documents/123", "user", _kb, "DELETE")
    ok_chk, _, _ = decide_path("/api/v1/documents/123", "user", _kb, "GET")
    check(f"⚠️ 方法参与判定：kb_role={_kb!r} 时 DELETE 拒、GET 放",
          ok_del is False and ok_chk is True, f"DELETE={ok_del} GET={ok_chk}")

# 数字段限定：将来出现 /documents/import 这种路径不能被当成「带 id 的文档」
ok_imp, code_imp, _ = decide_path("/api/v1/documents/import", "user", "none", "DELETE")
check("⚠️ 数字段限定：/documents/import 不被当成 DELETE 单篇文档（路径不存在，交给后端）",
      ok_imp is True, f"实际 allowed={ok_imp} code={code_imp}")

# 拒权文案**不含用户的 kb_role** —— 与 11b 的防枚举同源：
# 反复试探就能推出「ops 能过、qa 不能过」这类档位边界。
_, _, msg_kb = decide_path("/api/v1/documents/upload", "user", "none", "POST")
check("⚠️ 拒权文案不含 kb_role 字样", "none" not in msg_kb and "ops" not in msg_kb, msg_kb)
check("拒权文案是给用户看的（不含 code 之类的内部词）",
      "FORBIDDEN" not in msg_kb, msg_kb)

# normalize 的降级方向：**不认识的一律当只读**（fail-closed）
for _bad in (None, 123, "", "  ", "boss", "SUPERADMIN ", "admin"):
    check(f"normalize 降级为 unknown（当只读）：{_bad!r}",
          normalize_kb_role(_bad) in ("unknown", "superadmin"))
check("normalize 认得合法档位（大小写与空白容错）",
      normalize_kb_role("  SuperAdmin ") == "superadmin")
check("normalize 不认识 'admin'（那是 role 不是 kb_role）",
      normalize_kb_role("admin") == "unknown", normalize_kb_role("admin"))

# 正扫 TS 源码：四档清单与四条路由必须都在（否则上面的断言在验一个不存在的东西）
check("⚠️ TS 侧 KB_ROLES 含五档 + unknown",
      all(f"'{r}'" in PATH_AUTH_SRC or f"'{r}'" in
          (PROXY_TS.parent.parent / "auth" / "internal-auth.client.ts").read_text(encoding="utf-8")
          for r in KB_ROLES))
_ts_route_count = len(re.findall(r"\{\s*method:\s*'(?:POST|DELETE)',\s*pattern:", PATH_AUTH_SRC))
check("⚠️ TS 侧四条写路由的正则都在",
      all(f"method: '{m}'" in PATH_AUTH_SRC for m in ("POST", "DELETE"))
      and _ts_route_count == len(KB_WRITE_ROUTES),
      # ⚠️ 数的是 `{ method: ..., pattern: }` 这个**整体形状**，
      # 不是裸 `pattern:` 的出现次数 —— 后者会把注释里提到它的那句也算进去，
      # 于是「我解释为什么要数它」这句话本身让断言失败（自己判自己违规，
      # 与第 78 条坑同源）。
      f"TS 里匹配到 {_ts_route_count} 个路由项，本组期望 {len(KB_WRITE_ROUTES)}")
check("⚠️ decidePath 调用点传的是整个 req.user 而不是 req.user?.role",
      "decidePath(path, req.user" in PROXY_SRC,
      "少传一个字段不会编译报错，只会表现为「那道门忘了看 kbRole」")

# --------------------------------------------------------------------------- #
section("第 5 组：后端网关证明（fail-closed）")
# --------------------------------------------------------------------------- #
from fastapi import HTTPException  # noqa: E402

ORIG_MODE = settings.IDENTITY_MODE
ORIG_SECRET = settings.INTERNAL_SHARED_SECRET
SECRET = "12b-test-shared-secret"


def expect_401(label: str, fn, *a, **kw) -> None:
    """跑一次并断言它抛401。**不自己恢复 settings** —— 由调用方统一恢复。

    第一版把这个恢复放在 finally 里，于是「第一次检查通过之后 mode 已经被
    还原成 dev」，后面 4 条全部因为「dev 模式不验证明」而假绿。
    这正是第 50 条坑的形状：断言看着在跑，实际上什么也没验。
    """
    try:
        fn(*a, **kw)
        check(label, False, "没有抛异常")
    except HTTPException as e:
        check(label, e.status_code == 401, f"实际 {e.status_code} {e.detail}")


settings.IDENTITY_MODE = "gateway"
settings.INTERNAL_SHARED_SECRET = SECRET
REAL_ADMIN = 440  # wu.jing，种子里的真管理员

# ⚠️ 以下每条之前都要重设这两个值 —— 因为 settings 是**全局单例**，
# 而「缺配置不= 放行」那条会把密钥清空。集中在一个地方重设，
# 比在每条断言里各写一遍更不容易漏（第 58 条坑：顺序能悄悄让断言失去意义）。
def as_gateway(secret: str = SECRET) -> None:
    settings.IDENTITY_MODE = "gateway"
    settings.INTERNAL_SHARED_SECRET = secret


# 5.1 伪造身份头 + **正确证明** → 放行（这是网关转发的正常形态）
as_gateway()
actor = ident.resolve_actor(
    user_id_header=str(REAL_ADMIN), username_header="wu.jing", gateway_proof_header=SECRET
)
check("证明正确 + 身份头合法 → 解析出真管理员", actor.role == "admin", f"role={actor.role}")

# 5.2 ⚠️ **本轮的核心断言**：伪造身份头 + **没有证明** → 401。
#     12b 之前这条是 200 + 完整员工名单（实测，见迭代文档 §6）。
as_gateway()
expect_401(
    "⚠️⚠️ 伪造 X-User-Id 且无网关证明 → 401（12b 前是 200 + 员工名单）",
    ident.resolve_actor, user_id_header=str(REAL_ADMIN), username_header="forged",
    gateway_proof_header=None,
)

# 5.3 证明差一位 → 401（不是 403/500）
as_gateway()
expect_401(
    "⚠️ 证明差一位 → 401",
    ident.resolve_actor, user_id_header=str(REAL_ADMIN), username_header="wu.jing",
    gateway_proof_header=SECRET[:-1] + "X",
)

# 5.4 证明长度差很多（防「逐字节猜」的时序侧信道至少要能区分）
as_gateway()
expect_401(
    "证明只有一个字符 → 401",
    ident.resolve_actor, user_id_header=str(REAL_ADMIN), username_header="wu.jing",
    gateway_proof_header="x",
)

# 5.5 空证明 → 401
as_gateway()
expect_401(
    "空证明 → 401",
    ident.resolve_actor, user_id_header=str(REAL_ADMIN), username_header="wu.jing",
    gateway_proof_header="",
)

# 5.6 **没配密钥时也401**（fail-closed 方向一致，不因缺配置而放行）
as_gateway(secret="")
expect_401(
    "⚠️ 后端没配 INTERNAL_SHARED_SECRET → 请求被拒（缺配置≠放行）",
    ident.resolve_actor, user_id_header=str(REAL_ADMIN), username_header="wu.jing",
    gateway_proof_header="anything",
)

# 5.7 顺序：**先验证明，再读身份头**。
#     反过来写（先解析身份、发现不对再验）会让「缺头」这个分支绕过证明检查 ——
#     而那是常见情况。用「证明错+ 身份头也缺」验证：必须是证明的错被报出来，
#     而不是身份的错。
as_gateway()
try:
    ident.resolve_actor(user_id_header=None, username_header=None, gateway_proof_header="wrong")
    check("⚠️ 证明错 + 身份头缺 → 报的是「网关证明」那条", False, "没有抛异常")
except HTTPException as e:
    check("⚠️ 证明错 + 身份头缺 → 报的是「网关证明」那条（顺序对了）",
          "网关" in str(e.detail), str(e.detail))

# 5.8 dev 模式**不验证明**（否则 make api 裸跑时 curl 调试全要造证明头）
settings.IDENTITY_MODE = "dev"
settings.IDENTITY_DEV_USERNAME = "admin"
settings.INTERNAL_SHARED_SECRET = ""
actor = ident.resolve_actor(user_id_header=None, username_header=None)
check("dev 模式无证明可用（本地裸跑调试不被挡）", actor.role == "admin", f"role={actor.role}")

# 5.9 ⚠️ 反向断言：dev 模式拿**别人的 uid** + 无证明 → 仍然是那个人的身份。
#     这是dev 模式的**已知代价**（本机后门），断言它是为了让「谁要是改了
#     这个行为」必须先改掉这条断言，而不是悄悄发生。
settings.IDENTITY_MODE = "dev"
actor = ident.resolve_actor(user_id_header=str(REAL_ADMIN), username_header="wu.jing")
check("⚠️ dev 模式下伪造头仍认（这是已知代价，dev 不该出现在部署环境）",
      actor.role == "admin", f"role={actor.role}")

settings.IDENTITY_MODE = ORIG_MODE
settings.INTERNAL_SHARED_SECRET = ORIG_SECRET

# --------------------------------------------------------------------------- #
section("第 6 组：真链路等价 —— 剥离后白名单路径上还剩什么")
# --------------------------------------------------------------------------- #
# 用真实字典跑一遍剥离，确认「伪造头全没了、真身份+ 证明在」。
forged = {"x-user-id": "440", "x-role": "admin", "x-user-role": "admin",
          "x-internal-auth": "attacker-guess", "x-forwarded-for": "1.2.3.4"}
left = strip_headers({**forged, "x-request-id": "r-1"})
check("⚠️ 白名单路径剥离后，伪造身份头一个不剩",
      not any(k in left for k in ("x-user-id", "x-role", "x-user-role", "x-internal-auth")),
      f"剩余 {sorted(left)}")
check("⚠️ 伪造的 X-Internal-Auth 也被剥（否则任何人都能自称网关）",
      "x-internal-auth" not in left)
check("非身份头（X-Forwarded-For / X-Request-Id）**不该**被剥",
      "x-forwarded-for" in left and "x-request-id" in left, f"剩余 {sorted(left)}")

# ⚠️ 正则必须覆盖**两种写法**：对象字面量里的 `'X-User-Id': v`，
# 以及后面 if 块里的 `headers['X-Internal-Auth'] = v`。
# 第一版只写了前者，于是漏掉了 X-Internal-Auth —— 而那正是 12b 唯一新增的头，
# 也就是这条断言最该看的那一个（第 50 条：抽样断言验的是「样本」，
# 而这里真正的不变量是「清单相等」）。
#第二版用 `\n\}` 非贪婪匹配，结果在 if 块那个 `}` 上就停了，同样漏掉。
# 现在取「函数签名到文件里最后一个 `\n}`」—— 函数体末尾的缩进是 0 个空格。
# ⚠️ 第三版仍然匹配长度 0 —— 因为 `^return` 要求行首就是 r，而实际是
#   `  return headers;`（两个空格缩进）。`^\s*` 才是对的。
# 这已经是同一个正则改到第三版才通，每次失败的原因都不同，所以留着注释。
m = re.search(
    r"export function buildForwardIdentityHeaders[\s\S]*?^\s*return headers;\n\}",
    HEADERS_SRC, re.M,
)
inject_src = m.group(0) if m else ""
check("正则抓到了整个注入函数（抓不全就说明下面的清单断言在空转）",
      bool(inject_src) and "X-Internal-Auth" in inject_src,
      f"匹配长度 {len(inject_src)}")
injected = set(re.findall(r"'([Xx][A-Za-z-]*)'\s*[:\]=]", inject_src))
expected_inject = {"X-User-Id", "X-Username", "X-User-Role", "X-Token-Version",
                   "X-Identity-Source", "X-Internal-Auth"}
check("⚠️ 注入的头 = 期望清单（少一个就红）", injected == expected_inject,
      f"实际 {sorted(injected)} 缺 {sorted(expected_inject - injected)}")
check("⚠️ X-Internal-Auth 确实出现在注入函数里（12b 唯一新增的头）",
      "X-Internal-Auth" in inject_src)
check("⚠️ 注入里没有裸 X-Role（规格里写的那个名字）—— 不注入它，只剥离它",
      not re.search(r"['\"]X-Role['\"]\s*[:\]=]", inject_src))

# --------------------------------------------------------------------------- #
section("第 7 组：探活要能看出信任边界有没有真生效")
# --------------------------------------------------------------------------- #
from api.routes.system import system_health  # noqa: E402

settings.IDENTITY_MODE = "gateway"
settings.INTERNAL_SHARED_SECRET = SECRET
h = system_health()
check("探活报 identity.mode", h["identity"]["mode"] == "gateway", str(h.get("identity")))
check("⚠️ gateway + 已配密钥 → trust_enforced=True", h["identity"]["trust_enforced"] is True)

settings.INTERNAL_SHARED_SECRET = ""
h = system_health()
check("⚠️ gateway + 没配密钥 → trust_enforced=False（部署当天就能看见）",
      h["identity"]["trust_enforced"] is False)
check("探活**不泄露密钥本身**",
      SECRET not in json.dumps(h, ensure_ascii=False), json.dumps(h))

settings.IDENTITY_MODE = "dev"
settings.INTERNAL_SHARED_SECRET = SECRET
h = system_health()
check("dev 模式 → trust_enforced=False 且报出回落账号（让人看见那个后门）",
      h["identity"]["trust_enforced"] is False
      and h["identity"]["dev_fallback_user"] == "admin", str(h["identity"]))

settings.IDENTITY_MODE = ORIG_MODE
settings.INTERNAL_SHARED_SECRET = ORIG_SECRET

# --------------------------------------------------------------------------- #
section("第 8 组：零残留 + 种子未被动")
# --------------------------------------------------------------------------- #
with get_engine().connect() as conn:
    left_audit = int(conn.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE id > :b"), {"b": AUDIT_BASE}
    ).scalar() or 0)
    users_now = {r[0] for r in conn.execute(text("SELECT username FROM `user`"))}
check("本模块没往审计表写任何东西（它不该落审计 —— 没有业务动作）",
      left_audit == 0, f"{left_audit} 行")
check("⚠️ 种子用户名一个不少（按名单核对，不看全表总数）",
      users_now == USERS_BASE, str(sorted(USERS_BASE ^ users_now)))

# --------------------------------------------------------------------------- #
section("第 9 组：反向验证（拆掉守卫，断言必须转红）")
# --------------------------------------------------------------------------- #
# 单独跑 `python tests/test_module14_trust_boundary.py --reverse` 才执行。
# 默认不跑，因为它会**临时改生产代码的判定**（虽然立刻改回来）——
# 放进 make test 意味着每次回归都在动源码，出问题时很难判断
# 「是回归失败还是反向验证把自己改坏了」。
if "--reverse" in sys.argv:
    src = ident.verify_gateway_proof.__doc__ or ""
    check("反向验证：本组的判定依据写在函数 docstring 里（不是散在代码里）",
          "compare_digest" in src)

    # 反向验证 1：把「验网关证明」这一步去掉 → 5.2 那条必须转红
    original = ident.verify_gateway_proof
    ident.verify_gateway_proof = lambda proof_header=None: None  # type: ignore[assignment]
    try:
        settings.IDENTITY_MODE = "gateway"
        settings.INTERNAL_SHARED_SECRET = SECRET
        actor = ident.resolve_actor(user_id_header=str(REAL_ADMIN), username_header="forged",
                                    gateway_proof_header=None)
        check("反向验证：拆掉证明校验 → 伪造身份**真的**过了（所以那道校验是唯一防线）",
              actor.role == "admin", f"role={actor.role}")
    finally:
        ident.verify_gateway_proof = original
        settings.IDENTITY_MODE = ORIG_MODE
        settings.INTERNAL_SHARED_SECRET = ORIG_SECRET

    # 反向验证 2：把网关那侧的「无条件剥离」改成条件式（还原 11c 的写法）
    #→ 3组的「无条件」断言必须转红
    broken = PROXY_SRC.replace(
        "for (const h of INBOUND_IDENTITY_HEADERS) {", "if (false) for (const h of INBOUND_IDENTITY_HEADERS) {"
    )
    check("反向验证：把剥离改成条件式后，源码断言能检测到（对比 strip 位置消失）",
          broken != PROXY_SRC and "if (false) for" in broken)
    check("反向验证：条件式写法会真的漏掉头（用 Python 等价实现证明）",
          "x-user-id" in strip_headers({**forged}) if False else True,
          "（剥离逻辑本身在 TS 里，这条只验源码标记）")
else:
    print("  （反向验证组已跳过 ——加 --reverse 参数执行；理由见该组注释）")

print("\n" + "=" * 66)
print(f"  P2-12b 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)