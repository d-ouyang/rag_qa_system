# pyright: basic
"""
P2-14d 测试：主应用知识库页的**入口收敛**（前端按写权限显隐）。

    .venv/bin/python tests/test_module14d_frontend_gating.py
    .venv/bin/python tests/test_module14d_frontend_gating.py --reverse

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    api/routes/documents.py   GET /capabilities（判据全部由 kb_acl 派生）
    frontend/src/api/documents.ts        getMyCapabilities
    frontend/src/types.ts                KbCapabilitiesResponse
    frontend/src/stores/documents.ts     canUpload / canDelete + 写操作守卫
    frontend/src/views/KnowledgeView.vue上传区/ 重试 / 删除的显隐

--------------------------------------------------------------------------
为什么这一项要单独一个模块（而不是并进 14e）
--------------------------------------------------------------------------
14e 验的是「后端**真的**拒绝了越权写操作」—— 那是对外承诺的**安全**属性。
14d 验的是「界面**不给出**点了会失败的入口」—— 那是对内体验。

两者必须分开，因为它们会**分别地**坏：
    · 后端守卫在、前端没收敛 → 安全没问题，但只读员工看到一屏按钮，
      点一个吃一个 403（14e 全绿，14d 会红）；
    · 前端收敛了、后端守卫没挂 → 界面干干净净，但curl 一下就能改全公司知识库
      （14d 全绿，14e 会红）。
合在一起写成「一个大绿」就丢掉了这个区分能力。

--------------------------------------------------------------------------
本模块的判据为什么大半是「读源码」
--------------------------------------------------------------------------
与 12b / 14f 同源：要防的失效**不报错**。
    · 前端自己抄了一份五档表 → 加一档时忘了改前端，运营被无理由藏掉按钮；
    · `onDrop` 里的权限判断被删 → 上传区看不见了，但拖文件仍能上传；
    · store 的守卫被删 → 按钮在，但绕过视图直接调 store 就能发请求。
这些都不产生任何报错，只产生「看起来不太对」。

⚠️ 但**不是**全靠读源码：`/capabilities` 的判定与守卫的拒绝路径都跑真 HTTP
（第 2 组），所以「端点真的按档位返回」这件事有运行时证据。

--------------------------------------------------------------------------
⚠️ 写库说明（与 14a/14c 的差别要说清）
--------------------------------------------------------------------------
本模块**只写两个账号的 password_hash**，不碰任何业务数据：
不删文档、不改 kb_role、不动 session/chat_message。
这么写是因为「拿真 token 只能真登录」，而固定密码不能靠
`scripts/dev_test_accounts.py` 保证 —— `tests/acceptance_p2_12b.py`
的收尾会把那两个账号换成随机临时密码（它唯一合法的还原方式），
于是「依赖固定密码」的测试会**时绿时红**，而红的原因与被测物无关。
详见下面 `_strip_py_comments` 之后那段「自备凭据」的注释。

⚠️ 依赖：需要后端(8000) 与网关(3000) 在跑（`make dev` 或分别起）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EMBEDDING_BACKEND", "local")
os.environ.setdefault("RERANK_BACKEND", "local")

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0

GATEWAY = os.environ.get("GATEWAY_BASE", "http://127.0.0.1:3000")

DOCS_ROUTE = ROOT / "api" / "routes" / "documents.py"
API_TS = ROOT / "frontend" / "src" / "api" / "documents.ts"
TYPES_TS = ROOT / "frontend" / "src" / "types.ts"
STORE_TS = ROOT / "frontend" / "src" / "stores" / "documents.ts"
VIEW_VUE = ROOT / "frontend" / "src" / "views" / "KnowledgeView.vue"


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


def _script_of(vue_src: str) -> str:
    """只取 `<script setup>` 段。

    ⚠️ 为什么要切出来：判据只可能写在 script 里，而**整份 .vue 扫全文会咬住
    无关字面量** —— 第一版断言「视图里不该出现 'none'」就是这样红的：
    命中的是模板里那个上传图标的 `fill="none"`（SVG 属性，与权限毫无关系）。

    这与踩坑清单第 50 条同源：**判据不精确时，它红的原因不是被测物有问题**，
    而是自己撞上了别的东西 —— 而这种红会让人去改对的东西。
    """
    start = vue_src.find("<script setup")
    if start < 0:
        return vue_src
    end = vue_src.find("</script>", start)
    return vue_src[start:end] if end > start else vue_src[start:]


# --------------------------------------------------------------------------- #
# 自备凭据：为什么本模块必须自己设密码
# --------------------------------------------------------------------------- #
# ⚠️ **踩过的坑，别照抄「用dev_test_accounts 的固定密码」这种写法。**
# `tests/acceptance_p2_12b.py` 的 `restore_pwd()` 在收尾时会把账号换成
# **随机临时密码 + must_change_password=1**（这是它唯一合法的还原方式 ——
# 种子的原密码早已不可知）。于是：
#
#     跑完 make accept-12b  →  chen.jie / zhao.min 的固定密码就没了
#     → 任何依赖那两个固定密码的测试，从此开始随机 401
#
# 本模块第一版就是这么写的，结果它「时绿时红」，而红的原因跟被测物无关。
# 一个测试的成败取决于**跑它之前跑过什么**，那它测的就不是自己的被测物。
#
# 所以这里自己设密码（写库），收尾再换回随机临时密码。
# 代价是本模块**要写库** —— 但写的只有这两个账号的 password_hash，
# 不碰任何业务数据（不删文档、不改档位）。
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from core import password_policy as policy  # noqa: E402
from core.db import get_engine  # noqa: E402


def set_password(username: str, pwd: str) -> None:
    with get_engine().begin() as c:
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=0 "
                 "WHERE username=:u"),
            {"h": policy.hash_password(pwd), "u": username},
        )


def restore_password(username: str) -> None:
    """换回随机临时密码 + 强制改密。

    ⚠️ 这不是「假装还原」：种子里那两个账号的原始密码谁也不知道
    （`make seed-users`给的是一次性的随机值），所以换成另一个合法值
    是唯一诚实的还原。判据是「不再有人能拿固定密码登进去」。
    """
    with get_engine().begin() as c:
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=1 "
                 "WHERE username=:u"),
            {"h": policy.hash_password(policy.generate_temporary_password()), "u": username},
        )


# --------------------------------------------------------------------------- #
# HTTP 小工具（真链路，不用测试客户端）
# --------------------------------------------------------------------------- #
def http_json(
    method: str, path: str, *, body: dict | None = None, token: str | None = None
) -> tuple[int, dict]:
    url = path if path.startswith("http") else f"{GATEWAY}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # 连不上
        return 0, {"_error": str(e)}


#: 两个测试账号，**密码由本模块自己设**（见上面「自备凭据」段）。
#: 刻意挑**档位不同**的两个：只用一个账号的话，「按档位返回」这件事没判别力——
#: 返回 `upload=false` 和返回 `upload=true` 在「恰好等于期望」时才分得开，
#: 而只有一个样本时恒真的断言看起来也是绿的（踩坑清单第 50 条）。
#:
#: ⚠️ 期望档位写在ACCOUNTS 里，但它**不是判据的来源** —— 判据来自
#: `kb_acl.MIN_KB_ROLE_BY_ACTION`。所以如果有人改了 kb_acl（比如把 ops
#: 也给reindex），这里会红：那正是我们想要的（期望值该跟着改）。
#: 反过来如果这里写死「读库里是什么就是什么」，断言就恒真了。
ACCOUNTS = [
    ("zhao.min", "P2d14dZhao2026!Xx", "none",
     {"upload": False, "delete": False, "reindex": False}),
    ("chen.jie", "P2d14dChen2026!Xx", "superadmin",
     {"upload": True, "delete": True, "reindex": True}),
]

# --------------------------------------------------------------------------- #
# 第 1 组：/capabilities 端点的形状与判据来源
# --------------------------------------------------------------------------- #
section("第 1 组：`GET /capabilities` 的契约")

route_src = DOCS_ROUTE.read_text(encoding="utf-8")
view_src = VIEW_VUE.read_text(encoding="utf-8")
store_src = STORE_TS.read_text(encoding="utf-8")
api_src = API_TS.read_text(encoding="utf-8")
types_src = TYPES_TS.read_text(encoding="utf-8")

check("路由文件里声明了 /capabilities",
      '"/capabilities"' in route_src,
      "没找到路由声明")

# ⚠️ 判据必须**由 kb_acl 派生**。前端端点最危险的写法是
# `{"upload": actor.kb_role != "none", ...}` —— 那是把判据抄了第二遍，
# 而 kb_acl 的五档语义（ops 与 qa 的差别在 reindex）一旦调整就会漂。
cap_body = route_src[route_src.find("def my_kb_capabilities"):]
cap_body = cap_body[:cap_body.find("\n@router")] if "\n@router" in cap_body else cap_body
check("🔴 端点里的能力判定来自 kb_acl.capabilities（不是自己抄一份）",
      "kb_acl.capabilities(actor.kb_role)" in cap_body,
      cap_body[:300])
check("端点不自己写 if 判档位（不应出现 kb_role 的字符串比较）",
      not re.search(r"kb_role\s*(==|!=|in|not in)\s*[\"']", cap_body),
      "端点里出现了档位比较：" + str(re.findall(r"kb_role\s*(?:==|!=|in|not in)\s*[^)]*", cap_body)))

check("端点用 current_actor 而不是 require_kb_*（没权限的人也要能读它）",
      "Depends(current_actor)" in cap_body,
      "端点用了错误的依赖 —— 只读用户会拿到 403 而不是「知道自己不能」")

# 声明顺序：必须早于 `/{doc_id}/...`，否则会被路径参数吃掉
check("🔴 /capabilities 声明在 /{doc_id}/ 之前（否则被当成 doc_id=capabilities）",
      route_src.find('"/capabilities"') < route_src.find('"/{doc_id}/chunks"'),
      f"capabilities@{route_src.find(chr(34) + '/capabilities' + chr(34))} "
      f"chunks@{route_src.find(chr(34) + '/{doc_id}/chunks' + chr(34))}")

check("🔴 前端不许硬编码五档表（'superadmin' 之类字面量不出现在主应用里）",
      "superadmin" not in view_src and "superadmin" not in store_src,
      "主应用里出现了档位字面量 —— 判据被抄了第二份")
check("主应用也不自己判 'none'（那是后端 kb_acl 的语义）",
      not re.search(r"[\"']none[\"']", _script_of(view_src)),
      "视图的 script 段里出现了 'none' 字面量")

# --------------------------------------------------------------------------- #
# 第 2 组：真链路 —— 两个档位的实际返回
# --------------------------------------------------------------------------- #
section("第 2 组：真链路（经网关）两个档位的实际返回")

# 先自备凭据（见「自备凭据」段）：不依赖「跑之前没人动过这两个账号」这种运气
def _assert_capabilities(tokens: dict[str, str]) -> None:
    """第 2 组的真链路断言。单独一个函数是为了让上头的 finally 能收尾。"""

    if len(tokens) == len(ACCOUNTS):
        for username, _password, expect_role, expect_caps in ACCOUNTS:
            status, payload = http_json(
                "GET", "/api/v1/documents/capabilities", token=tokens[username]
            )
            check(f"{username} 读 /capabilities 返回 200", status == 200,
                  f"status={status} payload={str(payload)[:200]}")
            if status != 200:
                continue
            got_role = payload.get("kb_role")
            got_caps = payload.get("capabilities") or {}
            check(f"{username} 的档位是 {expect_role}（不是前端传什么就是什么）",
                  got_role == expect_role, f"期望={expect_role} 实际={got_role}")
            check(f"{username} 的能力判定与 kb_acl 一致：{expect_caps}",
                  {k: bool(got_caps.get(k)) for k in expect_caps} == expect_caps,
                  f"实际={got_caps}")
            check(f"{username} 拿到了中文 label（界面要显示「你当前是只读」）",
                  isinstance(payload.get("label"), str) and len(payload["label"]) > 0,
                  f"label={payload.get('label')!r}")
            # fail-closed：三个键必须**齐全**。少一个键时前端 `?? false` 会把它当无权限，
            # 表现是「有权限的人看不到按钮」—— 所以缺键也要红。
            check(f"{username} 的 capabilities 三个键齐全（前端按固定键读）",
                  set(got_caps) == {"upload", "delete", "reindex"},
                  f"实际键={sorted(got_caps)}")

        # 两个档位必须**真的不同** —— 只验「一个账号返回了期望值」的话，
        # 一个恒返回 upload=true 的端点也能在期望为 true 的那个账号上变绿。
        a = http_json("GET", "/api/v1/documents/capabilities", token=tokens["zhao.min"])[1]
        b = http_json("GET", "/api/v1/documents/capabilities", token=tokens["chen.jie"])[1]
        check("🔴 两个档位返回的能力不同（判据真的在按档位变化，不是常量）",
              (a.get("capabilities") or {}) != (b.get("capabilities") or {}),
              f"none={a.get('capabilities')} superadmin={b.get('capabilities')}")

        # 越权探测：只读用户打真写路由仍必须 403（证明「前端收敛」没有替代后端守卫）
        status, payload = http_json(
            "POST", "/api/v1/documents/upload/batch", body={}, token=tokens["zhao.min"]
        )
        check("🔴 只读用户打真写路由仍被拒（前端收敛没替代后端守卫）",
              status == 403,
              f"status={status} —— 若是 4xx 以外的值，说明守卫没生效或账号档位不对")
    else:
        # ⚠️ **不能静默跳过**。第一版这里是「登录失败就跳过第 2 组」，
        # 结果整个模块少了一半断言却依然报「全绿」——
        # 而登录失败最常见的原因是「后端没起」，那恰恰是最该红的时候。
        # 「跳过」与「通过」在退出码上无法区分，这是最坏的一种失败方式。
        check("🔴 两个账号都登录成功（否则第 2 组全部无从验起）",
              False,
              f"只拿到 {len(tokens)}/{len(ACCOUNTS)} 个 token —— "
              "检查后端(8000)与网关(3000)是否在跑")


# --------------------------------------------------------------------------- #
# 登录限流退避
# --------------------------------------------------------------------------- #
#: 网关对 `POST /api/auth/login` 的限流：**8 次 / 60 秒 / IP**（写死在
#: `auth.controller.ts` 的 `@Throttle`，刻意不可配置 —— 理由见那个文件的注释）。
LOGIN_THROTTLE_WINDOW_S = 62
LOGIN_THROTTLE_MAX_RETRY = 2


def _login_with_backoff(username: str, password: str) -> tuple[int, dict]:
    """登录，撞到 429 就等一个限流窗口再试。

    ⚠️ **为什么必须退避，而不是把 429 当失败**：这个模块一次要登 2 个账号，
    而 `make test` 里前后还有 12b / 13c 等也要真登录的模块。
    第一版没有退避，于是「连跑两遍」这条项目铁律**第二遍必红**，
    而红的原因是限流，与被测物无关 ——
    顺带把「跑第几遍会红」这件事变成了运气。

    这与「用错密码会锁账号 15 分钟」是同一类问题：**测试触发了真实的防护机制**。
    正确做法是让测试适应机制，而不是把机制关掉 ——
    为了让尺子准而把尺子砸了，得到的不是准，是「量不出东西」。
    """
    import time

    status, payload = 0, {}
    for attempt in range(LOGIN_THROTTLE_MAX_RETRY + 1):
        status, payload = http_json(
            "POST", "/api/auth/login", body={"username": username, "password": password}
        )
        if status != 429 or attempt == LOGIN_THROTTLE_MAX_RETRY:
            if status == 429:
                print(f"  （{username} 登录连撞 {LOGIN_THROTTLE_MAX_RETRY} 次限流，不再等"
                      " —— 若这条红，请隔一分钟再跑）")
            return status, payload
        print(f"  （{username} 撞上登录限流 8 次/分钟，等 {LOGIN_THROTTLE_WINDOW_S}s 后重试"
              f" [{attempt + 1}/{LOGIN_THROTTLE_MAX_RETRY}]）")
        time.sleep(LOGIN_THROTTLE_WINDOW_S)
    return status, payload


def _group2_token_asserts() -> None:
    """设密码 → 登录 → 验两个档位的返回 → **无论如何**换回随机临时密码。

    ⚠️ 整个流程包在函数里而不是散在模块级，是为了两件事：
      · `finally` 能可靠收尾（模块级代码里一个裸 `except` 就能跳过它，
        而「测试留下的东西」是下一次失败最难查的来源）；
      · 顺序可控 —— 模块级语句**按书写顺序执行**，
        调用点必须在两个被调函数的 `def` 之后，否则 `NameError`
        会把整个测试打断（第一版踩过两次）。
    """
    for _u, _p, _r, _c in ACCOUNTS:
        try:
            set_password(_u, _p)
        except Exception as _e:
            check(f"给 {_u} 设测试密码（需要 MySQL 在跑）", False, str(_e)[:200])

    tokens: dict[str, str] = {}
    try:
        for username, password, _role, _caps in ACCOUNTS:
            status, payload = _login_with_backoff(username, password)
            check(f"{username} 能登录（后端与网关在跑）",
                  status == 200 and "access_token" in payload,
                  f"status={status} payload={str(payload)[:200]}")
            if status == 200:
                tokens[username] = payload["access_token"]

        _assert_capabilities(tokens)
    finally:
        # ⚠️ 无论成败都要还原：留在库里等于给两个账号留了后门。
        for _u, _p, _r, _c in ACCOUNTS:
            try:
                restore_password(_u)
            except Exception:
                pass
        print("  （已把两个账号的密码换回随机临时密码 —— 与 acceptance_p2_12b 同一规格）")


# ⚠️ 调用点必须在 `_login_with_backoff` / `_assert_capabilities` 的定义之后。
_group2_token_asserts()


# --------------------------------------------------------------------------- #
# 第 3 组：前端三个文件的静态契约
# --------------------------------------------------------------------------- #
section("第 3 组：前端静态契约")

check("api 层有 getMyCapabilities（打的是 /api/v1/documents/capabilities）",
      "getMyCapabilities" in api_src and "/api/v1/documents/capabilities" in api_src,
      "api 层缺这个函数")

check("types 里有 KbCapabilitiesResponse（三个布尔 + 档位 + label）",
      "interface KbCapabilitiesResponse" in types_src
      and types_src.count("reindex: boolean") >= 1,
      "类型缺字段")

check("store 暴露 canUpload / canDelete（视图靠它们显隐）",
      "canUpload" in store_src and "canDelete" in store_src
      and "capabilities," in store_src,
      "store 没导出这些 getter")

# fail-closed 的形状：getter 必须 `?? false`，不能 `?? true`
cu = store_src[store_src.find("const canUpload"):]
cu = cu[:cu.find("\n\n")] if "\n\n" in cu else cu
check("🔴 拿不到权限时一律当无权限（?? false，不是 ?? true）",
      "capabilities.value?.capabilities.upload ?? false" in store_src
      and "capabilities.value?.capabilities.delete ?? false" in store_src,
      "fail-closed 的方向写反了 —— 请求失败时会给所有人显示写入口")

check("🔴 store 登出/重置时清掉 capabilities（否则下一位登录者继承上一位的权限）",
      re.search(r"function reset\(\)[^}]*capabilities\.value = null", store_src, re.S) is not None,
      "reset() 里没清 capabilities")

# 写操作的前端守卫（纵深防御：按钮藏了不等于不能调）
guard = store_src[store_src.find("async function upload"):]
guard = guard[:guard.find("async function download")] if "async function download" in guard else guard
check("🔴 upload / retry / remove 三个写操作都有前端权限守卫",
      guard.count("canUpload.value") + guard.count("canDelete.value") >= 3,
      f"守卫出现次数={guard.count('canUpload.value') + guard.count('canDelete.value')}（期望≥3）")

# 视图：上传区与两个按钮的显隐
check("视图用 docs.canUpload / docs.canDelete（不自己判档位）",
      "docs.canUpload" in view_src and "docs.canDelete" in view_src,
      "视图没读 store 的 getter")
check("🔴 上传区按权限显隐（v-if=\"canUpload\"，只读时换成说明卡）",
      'v-if="canUpload" class="card dropzone"' in view_src
      and "readonly-note" in view_src,
      "上传区没有按权限显隐")
check("删除按钮按 canDelete 显隐", 'v-if="canDelete" class="op-btn danger"' in view_src,
      "删除按钮没有按权限显隐")
check("重试按钮按 canUpload 显隐（后端 reparse 用的是 require_kb_upload）",
      'v-if="canUpload" class="op-btn" :disabled="!canRetry(d)"' in view_src,
      "重试按钮的权限条件不对 —— 它走的是 reparse，要求的是 upload 档")

# 🔴 拖拽的洞：@drop 挂在**整个 section** 上，所以「上传区不渲染」不等于拖不了。
# 只看模板会觉得已经收紧了；真正的判据是 onDrop 里有没有那个 if。
drop_fn = view_src[view_src.find("function onDrop"):]
drop_fn = drop_fn[:drop_fn.find("\n}")] if "\n}" in drop_fn else drop_fn
check("🔴 onDrop 里有权限判断（拖到页面任意空白处也会触发上传）",
      "canUpload.value" in drop_fn or "!canUpload" in drop_fn,
      "onDrop 没有判权限 —— 隐藏上传区之后仍能把文件拖进来上传")
check("onDrop 的权限判断在读取 files **之前**（否则已经准备提交了）",
      (drop_fn.find("canUpload") < drop_fn.find("dataTransfer")
       if "canUpload" in drop_fn and "dataTransfer" in drop_fn else False),
      f"判断@{drop_fn.find('canUpload')} 取files@{drop_fn.find('dataTransfer')}")

check("只读时给一句说明而不是留一个点不动的上传区",
      "readOnlyHint" in view_src and "只读" in view_src,
      "只读提示缺失")

# --------------------------------------------------------------------------- #
# 第 4 组：反向验证（--reverse 才跑：会临时改生产代码）
# --------------------------------------------------------------------------- #
section("第 4 组：反向验证（--reverse 才跑：会临时改生产代码）")

if "--reverse" in sys.argv:
    try:
        # 反向 1：把端点改成「自己抄一份档位表」→ 第 1 组的派生判据必须红
        check("反向验证：端点里那行 kb_acl.capabilities(...) 是可定位的（前提）",
              "kb_acl.capabilities(actor.kb_role)" in route_src)
        broken = route_src.replace(
            '"capabilities": kb_acl.capabilities(actor.kb_role),',
            '"capabilities": {"upload": actor.kb_role != "none", '
            '"delete": actor.kb_role != "none", "reindex": actor.kb_role != "none"},',
        )
        b_body = broken[broken.find("def my_kb_capabilities"):]
        check("反向验证：抄一份判据后，「判据来自 kb_acl」能检测到",
              broken != route_src
              and "kb_acl.capabilities(actor.kb_role)" not in b_body
              and re.search(r'kb_role\s*!=\s*"none"', b_body) is not None,
              "改写没生效或判据失效")

        # 反向 2：fail-closed 方向写反 → 第 3 组必须红
        check("反向验证：两个 ?? false 是可定位的（前提）",
              "capabilities.value?.capabilities.upload ?? false" in store_src)
        broken2 = store_src.replace(
            "capabilities.value?.capabilities.upload ?? false",
            "capabilities.value?.capabilities.upload ?? true",
        )
        check("反向验证：把 ?? false 改成 ?? true 后，fail-closed 判据能检测到",
              broken2 != store_src
              and "capabilities.value?.capabilities.upload ?? false" not in broken2,
              "替换没生效")

        # 反向 3：删掉 onDrop 的权限判断 → 「拖拽也有洞」那条必须红
        check("反向验证：onDrop 里有 canUpload 判断是可定位的（前提）",
              "canUpload.value" in drop_fn, drop_fn[:200])
        broken3 = view_src.replace(
            "  if (!canUpload.value) {\n"
            "    ui.toast('你的账号没有上传知识库文档的权限，如需上传请联系管理员', 'error', 5000)\n"
            "    return\n"
            "  }\n",
            "",
        )
        b_drop = broken3[broken3.find("function onDrop"):]
        b_drop = b_drop[:b_drop.find("\n}")] if "\n}" in b_drop else b_drop
        check("反向验证：删掉 onDrop 的判断后，「拖拽也有洞」能检测到",
              broken3 != view_src
              and "canUpload.value" not in b_drop,
              "替换没生效")

        # 反向 4：reset 不清 capabilities → 「下一位登录者继承权限」那条必须红
        check("反向验证：reset 里那行是可定位的（前提）",
              re.search(r"function reset\(\)[^}]*capabilities\.value = null", store_src, re.S)
              is not None)
        broken4 = re.sub(
            r"(function reset\(\)[^}]*?)capabilities\.value = null",
            r"\1",
            store_src,
            count=1,
            flags=re.S,
        )
        check("反向验证：reset 里删掉清理后，那条判据能检测到",
              broken4 != store_src
              and re.search(r"function reset\(\)[^}]*capabilities\.value = null",
                            broken4, re.S) is None,
              "替换没生效")

        # 反向 5：把上传区的 v-if 去掉 → 「上传区按权限显隐」必须红
        check("反向验证：上传区的 v-if=\"canUpload\" 是可定位的（前提）",
              'v-if="canUpload" class="card dropzone"' in view_src)
        broken5 = view_src.replace('v-if="canUpload" class="card dropzone"',
                                   'class="card dropzone"')
        check("反向验证：去掉 v-if 后，「上传区按权限显隐」能检测到",
              broken5 != view_src
              and 'v-if="canUpload" class="card dropzone"' not in broken5,
              "替换没生效")
    finally:
        print("  （反向验证组只改内存里的字符串，没有落盘 —— "
              "所以这一组不需要还原）")
else:
    print("  （反向验证组已跳过 —— 加 --reverse 参数执行；理由见该组注释）")

print("\n" + "=" * 66)
print(f"  P2-14d 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
