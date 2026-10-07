# pyright: basic
"""
P2-14e 知识库写权限越权回归。

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
`core/kb_acl.py` 的判据在14a 写完、`core/identity.py` 的三个 `require_kb_*`
守卫挂上四条写路由也在 14a 做完，但**这两件事都没有回归**。

14a 的验证是**人工枚举四条路由**做的 —— 那只能证明「今天这四条对了」，
证明不了两件事：

    ① 将来有人**加第五条写路由**而忘了挂守卫 → 矩阵断言照样全绿，
       而那一条是**裸的**（上传接口无门，全公司知识库对所有人敞开）；
    ② 守卫挂上了，但**拒权路径本身抛异常** → 判定是对的，
       用户拿到的是 500 而不是 403（14a 真的踩过：见 `kb_acl.describe`
       文件头）。

所以本模块的三组断言分别对应上面三个洞。

--------------------------------------------------------------------------
第一组：矩阵断言（四条路由 × 五档 kb_role）
--------------------------------------------------------------------------
逐格断「该拒的拒、该放的放」。

⚠️ **必须成对**，与 module15 同一条道理：
「`none` 被拒」这条断言，在**守卫根本没挂**时也会绿 —— 因为那时接口
会因为别的原因（文件不存在 / 参数不对）返回 400/404/422。
所以每组都要配一条「有权限的档确实能过这道依赖」的断言。
只有前者是「拦住了」，只有后者是「拦对了、没拦过头」。

跑法上有个刻意的取舍：**用真实 `Actor` 对象 + 直接调依赖函数**，
而不是打真 HTTP 上传文件。理由：

    · 真上传会在`upload/` 留文件、在 `document` 表留行——
      14a已经栽过两次（见 PLAN §4.8），测试不该再往真知识库里造数据；
    · 守卫是纯函数式的（`actor.kb_role` → 判定），不碰库，
      所以「守卫判得对不对」这件事**不需要真库就能验**；
    · 真的 HTTP 链路在 14a / 14b 的真链路实测里验过两遍了。

那三条真链路（真进程、真令牌、真上传文件）由
`scripts/` 下的验收脚本与迭代文档的验证章节负责，不在本模块。

--------------------------------------------------------------------------
第二组：结构断言（本项独有，🔴 最重要）
--------------------------------------------------------------------------
扫 `api/routes/documents.py` 的**源码**，把每一条
`@router.post / put / patch / delete` 连同它的 `Depends` 列表一起提出来，
逐条核对：

    每一条写路由的依赖里必须含 `require_kb_*`；
    且挂的守卫要与该路由的语义对上（上传类→ upload，删除→ delete）；
    且写路由的条数必须等于 14a 认定的那四条（多一条就红）。

**为什么这条是本项存在的理由**：14.0 的实测基线写得很清楚 ——
「本项不是加一层校验，而是从零补上整个守卫」，
失败模式是「**漏一个路由就完全没有防护**，而且不会有任何报错提示」。

矩阵断言**结构上无法**覆盖这个洞：它只认得已枚举的那四条，
第五条写路由对它完全隐形。所以必须有一条**「从源码里发现写路由」**
的断言 —— 发现的范围由源码决定，不由枚举决定。

⚠️ 这条断言也是**双向**的：
    · 正向：现在这四条都必须挂上守卫；
    · **反向（更重要的那条）**：把某一条的 `Depends` 去掉，
      这条断言必须转红（`--reverse` 跑的就是这个）。
正向只能证明「今天对了」，反向才能证明「它真的会红」——
一条永远不会红的断言等于没有断言。

--------------------------------------------------------------------------
第三组：「拒权路径本身要能正常返回 403」
--------------------------------------------------------------------------
独立于「权限判得对不对」的一条断言，14a 真的踩过：
`kb_acl.describe(kb_role)` 参数名写成了 `kb_role` 而函数体用 `action`
→ 每次拒权都变成 500 `NameError`。日志里明明打出
「知识库写操作被拒」，响应却是 500。

那个 bug 的危险形状是：**判定是对的，所以「普通员工上传不成功」这种
粗粒度观察照样成立**。只看「没上传成功」就收工，会以为守卫已做完，
而前端拿到 500 完全不知道是权限问题。

所以这一组直接对 `require_kb_*` 的依赖函数断言：
    有权限 → 返回 actor，不抛；
    无权限 → 抛 `HTTPException`，`status_code == 403`，且 `detail`
             是那句不含档位名的中文文案。

--------------------------------------------------------------------------
跑之前必须先 `make kb-guard-count`
--------------------------------------------------------------------------
2026-10-07 两次「跑回归把知识库清空」（PLAN §4.8）。
本模块**不写库**，但仍然按规矩跑：护栏是给自己看的，
确认基线是35/35 才好判断跑完之后是不是还是 35/35。

运行：
    make infra && make kb-guard-count
    .venv/bin/python tests/test_module12_write_acl.py

反向验证：
    .venv/bin/python tests/test_module12_write_acl_reverse.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


# --------------------------------------------------------------------------- #
# 导入被测对象
# --------------------------------------------------------------------------- #
from fastapi import HTTPException  # noqa: E402

from core import kb_acl  # noqa: E402
from core.identity import Actor, require_kb_delete, require_kb_reindex, require_kb_upload  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
DOCUMENTS_PY = REPO_ROOT / "api" / "routes" / "documents.py"
GATEWAY_AUTH_TS = REPO_ROOT / "gateway" / "src" / "auth" / "path-authorization.ts"

#: 本模块造出来的 `Actor` 用的假 id —— **不落到库里**。
#: 刻意用不存在的 id（种子员工是 101~110，见 scripts/dev_test_accounts.py）：
#: 万一哪个断言不小心把 actor 送进了审计写入，`audit_log` 里出现的
#: 是一个查无此人的 id，收尾时按 id 清单清理即可，不会误删别人的日志。
FAKE_USER_ID = 999001

#: 五档角色 → 期望能做什么。**这是 14a 定的矩阵，本模块只复述它**。
#:
#: ⚠️ 「期望」写在这里而不是从 `kb_acl` 派生，是**故意的**：
#: 若从 `MIN_KB_ROLE_BY_ACTION` 派生，这条断言就成了
#: 「实现等于它自己」—— 恒真、零判别力（这正是 module15 记的
#: 「验收脚本自己会骗人」的同一种病）。
#: 代价是改档位时要改两处；收益是改错档位时测试会红。
EXPECTED = {
    "none": {"upload": False, "delete": False, "reindex": False},
    "ops": {"upload": True, "delete": True, "reindex": False},
    "qa": {"upload": True, "delete": True, "reindex": True},
    "dev": {"upload": True, "delete": True, "reindex": True},
    "superadmin": {"upload": True, "delete": True, "reindex": True},
}


def make_actor(kb_role: str) -> Actor:
    """造一个带指定 `kb_role` 的 `Actor`。**纯内存对象，不落库。**"""
    return Actor(
        id=FAKE_USER_ID,
        username=f"probe.{kb_role}",
        display_name=f"探针_{kb_role}",
        role="user",
        kb_role=kb_role,
        status="active",
        source="header",
        record=None,
        client_ip="127.0.0.1",
    )


# 动作 → 该用哪个守卫依赖。
#: 映射的依据是**语义**（这个操作在动什么），不是路由名字 ——
#: 路由名会改，语义不会。
ACTION_DEP = {
    kb_acl.ACTION_UPLOAD: require_kb_upload,
    kb_acl.ACTION_DELETE: require_kb_delete,
    kb_acl.ACTION_REINDEX: require_kb_reindex,
}

# --------------------------------------------------------------------------- #
# 打印五档矩阵（人读的输出，顺便让判据可见）
# --------------------------------------------------------------------------- #
print("== 五档权限矩阵（实测 vs 期望） ==")
print(f"  {'kb_role':<12}{'upload':<9}{'delete':<9}{'reindex':<9}")
for role in sorted(EXPECTED):
    caps = kb_acl.capabilities(role)
    print(f"  {role:<12}{str(caps['upload']):<9}{str(caps['delete']):<9}{str(caps['reindex']):<9}")


# --------------------------------------------------------------------------- #
# 第一组：矩阵断言（逐格）
# --------------------------------------------------------------------------- #
print("\n== 第1 组：五档 × 三动作 矩阵断言 ==")

for role in sorted(EXPECTED):
    for action in sorted(EXPECTED[role]):
        want = EXPECTED[role][action]
        got = kb_acl.can(role, action)
        check(f"{role:<12}can_{action:<8}= {str(got):<5}（期望 {want}）", got == want,
              f"期望 {want} 实际 {got}")

print("\n== 第 1b 组：capabilities() 与逐个 can_* 必须一致 ==")
# capabilities() 是 14d 前端与 14f 管理端的唯一数据源。
# 它与逐个 can_* 分开写的话，加一档中间档时一定会有一处漏改。
for role in sorted(EXPECTED):
    caps = kb_acl.capabilities(role)
    for action in sorted(caps):
        single = kb_acl.can(role, action)
        check(f"capabilities({role})[{action}] 与 can() 一致", caps[action] == single,
              f"capabilities={caps[action]} can={single}")

print("\n== 第 1c 组：Actor 的三个 property 必须与 kb_acl 一致 ==")
# Actor.can_upload_document 等三个 property 是 14c 路由里真正读的东西。
# 它们与 kb_acl 若各写一份判据，就是「登录判定在网关与后端各写一份」的翻版。
for role in sorted(EXPECTED):
    a = make_actor(role)
    check(f"Actor(kb_role={role}).can_upload_document == can_upload",
          a.can_upload_document == kb_acl.can_upload(role),
          f"{a.can_upload_document} vs {kb_acl.can_upload(role)}")
    check(f"Actor(kb_role={role}).can_delete_document == can_delete",
          a.can_delete_document == kb_acl.can_delete(role),
          f"{a.can_delete_document} vs {kb_acl.can_delete(role)}")
    check(f"Actor(kb_role={role}).can_reindex_kb == can_reindex",
          a.can_reindex_kb == kb_acl.can_reindex(role),
          f"{a.can_reindex_kb} vs {kb_acl.can_reindex(role)}")

print("\n== 第 1d 组：fail-closed 不变量 ==")
# 「判不出身份 → 什么都不给」是整个权限模型的地基。
# 下面这些输入形态在真实链路里都出现过：库里那一列被手工 SQL 写坏、
# 老 token 没有这个字段、网关注入的头缺失（12b 会无条件剥掉伪造头）。
for bad in (None, "", "  ", "boss", "SUPERADMIN_TYPO", 0, 1, [], {}, "超管", "none "):
    norm = kb_acl.normalize(bad)
    is_none = (norm == kb_acl.KB_ROLE_NONE)
    # 唯一例外："none " 带尾空格 —— normalize 会 strip 成 "none"，
    # 那是**有意的规整**（手工 SQL 很容易带空格），不是降级。
    if isinstance(bad, str) and bad.strip().lower() == "none":
        check(f"normalize({bad!r}) → {norm!r}（尾空格被规整，不是降级）",
              norm == "none", f"实际 {norm!r}")
    else:
        check(f"normalize({bad!r}) → {norm!r}（fail-closed）", is_none,
              f"实际 {norm!r}")
    check(f"can_{'upload'}({bad!r}) = False（拿不到身份就不给权限）",
          kb_acl.can_upload(bad) is False, "")

print("\n== 第 1e 组：写路径要「响」，读路径要「静默」 ==")
# normalize 与 check_kb_role 的分工：读永不抛，写必抛。
# 反过来（读抛 / 写静默）的后果：一个空格就能让全公司登不上；
# 而管理员打了 'boss' 却显示成功，两边都错。
for bad in ("boss", "", "超管", "admin"):
    try:
        kb_acl.check_kb_role(bad)
        check(f"check_kb_role({bad!r}) 应抛 ValueError", False, "居然通过了")
    except ValueError:
        check(f"check_kb_role({bad!r}) 抛 ValueError（写入路径要响）", True)
for good in sorted(kb_acl.KB_ROLES):
    try:
        got = kb_acl.check_kb_role(good)
        check(f"check_kb_role({good!r}) → {got!r}", got == good)
    except ValueError as e:
        check(f"check_kb_role({good!r}) 不该抛", False, str(e))
# 大小写与空格在写入路径也要被规整掉（'Ops ' 是手工 SQL 的常见产物）
check("check_kb_role(' Ops ') → 'ops'（写入路径也规整）",
      kb_acl.check_kb_role(" Ops ") == "ops", "")
check("check_kb_role('SUPERADMIN') → 'superadmin'",
      kb_acl.check_kb_role("SUPERADMIN") == "superadmin", "")

print("\n== 第 1f 组：未知的 action 一律拒绝，而不是抛异常 ==")
# 症状设计的理由：调用方拼错动作名的症状应该是「这条操作被拒 + 日志里有记录」，
# 而不是 500。权限系统不该因为一个拼写错误就整个挂掉。
for bad_action in ("uplaod", "UPLOAD", "delete_all", "", "reindex_all", None):
    check(f"can('superadmin', {bad_action!r}) = False（未知动作拒绝）",
          kb_acl.can("superadmin", bad_action) is False, "")

print("\n== 第 1g 组：kb_role 与 role 正交（两个维度互不连坐） ==")
# 15 种组合全部合法。这一组断的是「加 kb_role 没有污染 role 的判据」——
# 那是14a 选独立字段的核心理由（塞进 role 会让管理端权限一起漂）。
from core import identity as identity_mod  # noqa: E402
from core.user_repo import ROLE_ADMIN, ROLE_HR  # noqa: E402

for role in (ROLE_ADMIN, ROLE_HR, "user"):
    for kb_role in sorted(EXPECTED):
        a = Actor(
            id=FAKE_USER_ID, username="x", display_name="x", role=role,
            kb_role=kb_role, status="active", source="header", record=None,
        )
        # role 判据不受 kb_role 影响
        check(f"role={role:<5} kb_role={kb_role:<11} can_staff 不被 kb_role 影响",
              a.can_staff == (role in identity_mod.STAFF_ROLES),
              f"can_staff={a.can_staff}")
# 反向：kb_role=superadmin 但 role=user → 能改知识库、进不了管理端
a = Actor(id=FAKE_USER_ID, username="x", display_name="x", role="user",
          kb_role="superadmin", status="active", source="header", record=None)
check("role=user + kb_role=superadmin → 能删知识库，但进不了管理端",
      (a.can_delete_document is True) and (a.can_staff is False),
      f"delete={a.can_delete_document} staff={a.can_staff}")


# --------------------------------------------------------------------------- #
# 第三组：拒权路径本身要能正常返回 403
# --------------------------------------------------------------------------- #
# ⚠️ 放在结构断言之前跑：它是14a 真踩过的坑（describe 参数名写错 → 500）。
# 「判定是对的」与「拒权能正常返回」是**两件独立的事**。
print("\n== 第 3 组：守卫依赖本身的行为（拒权必须 403，不能 500） ==")

for action, dep in sorted(ACTION_DEP.items()):
    dep_name = getattr(dep, "__name__", "?")

    # 3a有权限 → 返回 actor，不抛
    for role in sorted(k for k, v in EXPECTED.items() if v[action]):
        actor = make_actor(role)
        try:
            got = dep(actor)
            check(f"{dep_name} 放行 {role}", got is actor,
                  f"返回了 {got!r}，期望原 actor")
        except HTTPException as e:
            check(f"{dep_name} 放行 {role}", False,
                  f"竟抛了 {e.status_code}：{e.detail}")

    # 3b 无权限 → 403 + 文案不含档位名
    for role in sorted(k for k, v in EXPECTED.items() if not v[action]):
        actor = make_actor(role)
        try:
            dep(actor)
            check(f"{dep_name} 拒绝 {role}", False, "居然放行了")
        except HTTPException as e:
            check(f"{dep_name} 拒绝 {role} → 403（不是 500）",
                  e.status_code == 403, f"实际 {e.status_code}")
            check(f"{dep_name} 拒绝 {role} → 有中文文案", bool(e.detail),
                  "detail 为空")
            # 防枚举：文案不能说「你的档位是 none」
            check(f"{dep_name} 拒绝 {role} → 文案不含档位名",
                  role not in str(e.detail), f"文案里出现了 {role!r}：{e.detail}")

# 3c 拒权文案的「不含档位名」要在所有动作上成立
print("\n== 第 3b 组：拒权文案统一检查 ==")
for action in sorted(kb_acl.ACTIONS):
    text = kb_acl.describe(action)
    for role in sorted(kb_acl.KB_ROLES):
        check(f"describe({action}) 不含档位名 {role!r}", role not in text,
              f"文案：{text}")
    check(f"describe({action}) 有内容", bool(text.strip()), "")


# --------------------------------------------------------------------------- #
# 第二组：结构断言（🔴 本项的核心）
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：结构断言（扫源码，写路由必须挂守卫） ==")

DOC_SRC = DOCUMENTS_PY.read_text(encoding="utf-8")
DOC_LINES = DOC_SRC.split("\n")

#: 写方法的集合。**刻意不含 get** —— 读接口一律不加守卫，
#: 那是业务决策（知识库全公司共用，见设计规格 §2）。
WRITE_METHODS = ("post", "put", "patch", "delete")

#: 14a 认定的那四条写路由（方法 + 路径）。**这份枚举是「白名单」，
#: 用来发现「多出来的写路由」** —— 而下面的 `require_kb_*` 核对
#: 才是发现「漏挂守卫」。两个方向缺一不可。
KNOWN_WRITE_ROUTES = {
    ("post", "/upload"): "upload",
    ("post", "/upload/batch"): "upload",
    ("post", "/{doc_id}/reparse"): "upload",
    ("delete", "/{doc_id}"): "delete",
}


def _strip_py_comments(src: str) -> str:
    """去掉 Python 源码里的注释（`#` 到行尾）与三引号字符串内容，**保留行结构**。

    ⚠️ 不能用朴素的「按 # 切半行」：字符串里出现 `#`（URL 片段、颜色值 `#00E0A4`）
    会被当成注释，把后面真正的代码一起吃掉 —— 那样提取器会**少**报依赖，
    而「少报」在这类结构断言里等于「漏检」（踩坑清单第 12 条的形状）。

    所以这里走 `tokenize`：它知道什么是字符串、什么是注释。
    保留换行是为了让行号与原文仍然对得上（错误信息里要能报行号）。
    """
    import io
    import tokenize

    out: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                # 用等量空行占位，保持行数不变
                out.append("\n" * tok.string.count("\n"))
            else:
                out.append(tok.string)
    except tokenize.TokenError:
        # 源码被截断（tokenize 遇不到结尾）时宁可保守：返回原文，
        # 让提取器照常工作 —— 这时「多报」比「漏报」安全
        return src
    return "".join(out)


def extract_routes(src: str) -> list[tuple[str, str, str, int]]:
    """
    从路由源码里提出 `(方法, 路径, Depends 列表原文, 行号)`。

    用「装饰器块」切分而不是逐行正则：FastAPI 的装饰器可以跨行写，
    逐行正则会在参数换行时漏掉半个路由 —— 而**漏掉就等于没检查**，
    是这类结构断言最坏的失败方式（看起来通过了）。
    """
    lines = src.split("\n")
    marks: list[int] = []
    for i, line in enumerate(lines):
        if re.match(r"^@router\.(get|post|put|patch|delete)\(", line):
            marks.append(i)

    out: list[tuple[str, str, str, int]] = []
    for idx, start in enumerate(marks):
        end = marks[idx + 1] if idx + 1 < len(marks) else len(lines)
        block = "\n".join(lines[start:end])

        m = re.match(r"^@router\.(get|post|put|patch|delete)\(", lines[start])
        method = m.group(1) if m else "?"

        # 路径是装饰器里的第一个字符串字面量
        pm = re.search(r"""["'](\/[^"']*)["']""", block)
        path = pm.group(1) if pm else "?"

        # ⚠️ **必须先剥注释再找Depends**（14d 实测踩出来的）。
        # 依赖提取的正则是 `Depends\(\s*(\w+)` —— 它不区分代码与注释，
        # 于是一条在 docstring 里写着「真正的拒绝发生在
        # `Depends(require_kb_*)` 上」的读路由，会被提取出
        # `require_kb_` 这个依赖，然后「读路由不该挂 kb 守卫」那条转红。
        #
        # 症状极像真回归（一个 GET 路由真的挂了守卫？），而根因是提取器，
        # 修法必须是**让提取器看不见注释** ——
        # 加白名单「这几个依赖名算注释」只能救这一处，
        # 下一个人写一句提到别的依赖的 docstring 就又红了。
        code_only = _strip_py_comments(block)
        deps = " ".join(re.findall(r"Depends\(\s*([A-Za-z_][A-Za-z0-9_]*)", code_only))
        out.append((method, path, deps, start + 1))
    return out


routes = extract_routes(DOC_SRC)
write_routes = [r for r in routes if r[0] in WRITE_METHODS]
read_routes = [r for r in routes if r[0] == "get"]

print(f"  （扫到写路由 {len(write_routes)} 条、读路由 {len(read_routes)} 条）")

# 2-pre提取器自检：_strip_py_comments 生效，但**没有把真依赖一起吃掉**
# ---------------------------------------------------------------------------
# 为什么这三条必须在这里（而不是跑完再手工验一遍）：
# 「剥注释」这个机制最坏的失效方式是**剥过头** —— 一旦它把代码也当成注释，
# 提取器会报不出任何依赖，于是「每条写路由都挂了守卫」全部转红，
# 而红的原因是提取器坏了、不是守卫没了。这类失效每次都会发生。
# 所以它的判别力必须被钉住，且钉在**每次回归**里。
print("\n-- 2pre 依赖提取器：剥注释但不误伤代码 --")
_strip = _strip_py_comments
_extract = lambda s: re.findall(  # noqa: E731
    r"Depends\(\s*([A-Za-z_][A-Za-z0-9_]*)", s
)

# ① 注释/docstring 里的 Depends(名字) 不该被当成真依赖
check("注释里的 Depends(require_kb_*) 不会被提取成依赖",
      "require_kb_" not in _extract(
          _strip('def f(a: A = Depends(current_actor)):\n'
                 '    """判据在 Depends(require_kb_*) 上"""\n    pass')),
      _extract(_strip('def f(a: A = Depends(current_actor)):\n'
                      '    """Depends(require_kb_*)"""\n    pass')))

# ② 真依赖仍然提取得到（剥过头这条失效的红）
check("真依赖仍然提取得到（剥注释没有把代码也吃掉）",
      _extract(_strip("def f(a: A = Depends(require_kb_upload)):\n    pass"))
      == ["require_kb_upload"],
      _extract(_strip("def f(a: A = Depends(require_kb_upload)):\n    pass")))

# ③ 字符串里的 # 不是注释 —— 朴素的「按 # 切半行」会吃掉后面的代码，
#    而「提取不到依赖」在这类结构断言里等于**漏检**（比误报更坏）
check("字符串里的 # 不会被当成注释（否则后面的真依赖被吃掉 → 漏检）",
      _extract(_strip("s = 'color #00E0A4'\n"
                      "def f(a: A = Depends(require_kb_delete)):\n    pass"))
      == ["require_kb_delete"],
      _extract(_strip("s = 'color #00E0A4'\n"
                      "def f(a: A = Depends(require_kb_delete)):\n    pass")))

# ④ 行数不变（错误信息里要报得出真实行号）
check("_strip_py_comments 保留行数（否则报行号会错位）",
      _strip("a = 1\n# c\nb = 2\n").count("\n") == "a = 1\n# c\nb = 2\n".count("\n"),
      f"剥后行数={_strip('a = 1' + chr(10) + '# c' + chr(10) + 'b = 2' + chr(10)).count(chr(10))}")


# 2a 每一条写路由的依赖里必须有 require_kb_*
print("\n-- 2a 每条写路由都挂了 kb 守卫 --")
for method, path, deps, lineno in write_routes:
    check(f"L{lineno} {method.upper():<6} {path:<22} 依赖含 require_kb_*",
          "require_kb_" in deps, f"实际依赖：{deps!r}")

# 2b 守卫要与语义对上（上传类→upload，删除→delete）
print("\n-- 2b 守卫与路由语义对上 --")
for method, path, deps, lineno in write_routes:
    key = (method, path)
    if key not in KNOWN_WRITE_ROUTES:
        # 新增的写路由：不在 14a 的枚举里。这条断言转红是**设计如此** ——
        # 有人加了新写路由，必须有人来判断「它该挂哪个守卫」并更新这份枚举。
        # 漏挂守卫的路径在这个分支之前（2a）就已经红了。
        check(f"L{lineno} {method.upper():<6} {path:<22} 是 14a 枚举之外的新写路由",
              False, "请判断它该挂哪个守卫，并更新 KNOWN_WRITE_ROUTES")
        continue
    want_dep = f"require_kb_{KNOWN_WRITE_ROUTES[key]}"
    check(f"L{lineno} {method.upper():<6} {path:<22} 挂 {want_dep}",
          want_dep in deps, f"实际依赖：{deps!r}")

# 2c 写路由条数必须与 14a 的认定一致（多一条就红 —— 理由见上面）
found_write = {(m, p) for m, p, _, _ in write_routes}
check(f"写路由共 {len(KNOWN_WRITE_ROUTES)} 条（14a 认定值）",
      found_write == set(KNOWN_WRITE_ROUTES),
      f"实际：{sorted(found_write - set(KNOWN_WRITE_ROUTES))} 多出 / "
      f"{sorted(set(KNOWN_WRITE_ROUTES) - found_write)} 丢失")

# 2d 读路由一律不得挂 kb 守卫（业务决策：知识库共用，不隔离读）
print("\n-- 2d 读路由不得挂 kb 守卫 --")
for method, path, deps, lineno in read_routes:
    check(f"L{lineno} GET    {path:<22} 未挂 require_kb_*",
          "require_kb_" not in deps, f"实际依赖：{deps!r}")

# 2e 守卫必须挂在 Depends 里，而不是写在函数体里
#这是 14a 立下的规矩：判据 = 这张 Depends 列表，
# 挪进函数体的话结构断言就失去唯一可靠的机械信号。
print("\n-- 2e 守卫在 Depends 里，不在函数体里 --")
for method, path, deps, lineno in write_routes:
    if "require_kb_" not in deps:
        continue  # 2a 已经红了，这条跳过
    # 守卫函数体内若出现裸的 kb_acl.can( 调用 → 说明有人在函数体里又判了一次
    end = next((i for i, l in enumerate(DOC_LINES)
                if i + 1 > lineno and re.match(r"^@router\.", l)), len(DOC_LINES))
    body = "\n".join(DOC_LINES[lineno:end])
    check(f"L{lineno} {method.upper():<6} {path:<22} 函数体内无裸 kb_acl.can 调用",
          "kb_acl.can(" not in body,
          "判定应只在 require_kb_* 里，函数体内再判一次就成了两个真相源")

# 2f require_kb_reindex 目前没有任何路由使用 —— 这是**已知状态**，不是漏挂
print("\n-- 2f reindex 守卫当前无路由使用（整库重灌是脚本，不在 HTTP 面上）--")
used_deps = " ".join(d for _, _, d, _ in write_routes)
check("require_kb_reindex 未被任何写路由使用（符合14a 的已知状态）",
      "require_kb_reindex" not in used_deps,
      "如果将来 reindex 变成接口，它必须挂上这个守卫")
check("require_kb_reindex 本身可用（备好等它变成接口）",
      callable(require_kb_reindex), "")

# 2g 网关那份清单必须与后端这份一致（14b 的遗留项：各写一份、靠人同步）
print("\n-- 2g 网关 KB_WRITE_ROUTES 与后端写路由一致 --")
GW_SRC = GATEWAY_AUTH_TS.read_text(encoding="utf-8")
gw_block = re.search(r"KB_WRITE_ROUTES[^=]*=\s*\[(.*?)\n\];", GW_SRC, re.S)
check("网关源码里找得到 KB_WRITE_ROUTES", gw_block is not None,
      "路径或常量名变了 —— 网关粗筛失效，两侧要同步改")

if gw_block:
    # ⚠️ 解析 `{ method: 'POST', pattern: /^...$/ }` 四元组，**不硬编码字符串比对**。
    # 上一版硬编码了 `method: 'post'`（小写）与未转义的正则，而网关源码写的是
    # 大写方法名 + `\/` 转义 → 四条全假红。这类「尺子坏了」的失败最坏的地方在于
    # 它看起来像「实现有 bug」，而真去改网关只会把正确的东西改坏。
    gw_entries = re.findall(
        r"\{\s*method:\s*'(\w+)'\s*,\s*pattern:\s*/(.+?)/\s*\}", gw_block.group(1)
    )
    gw_set = {(m.lower(), p) for m, p in gw_entries}

    # 后端四条 → 网关正则里应有的样子（`{doc_id}` 是数字段）
    # ⚠️ 键是「方法」而不是「(方法, 正则)」元组：第一版把键写成元组，
    # 于是 `for method, pattern in d` 解包出的是**两个字符串**，
    # 再 `method[0]` 就取到了首字母 —— 断言名印出「D」而不是「DELETE」。
    # 这种错不会让测试红得明显，只会让四条断言一起假红，看着像实现有问题。
    backend_to_gw = {
        "post": {
            r"^\/api\/v1\/documents\/upload$",
            r"^\/api\/v1\/documents\/upload\/batch$",
            r"^\/api\/v1\/documents\/\d+\/reparse$",
        },
        "delete": {
            r"^\/api\/v1\/documents\/\d+$",
        },
    }
    expected_gw = {(m, p) for m, pats in backend_to_gw.items() for p in pats}
    for method, pattern in sorted(expected_gw):
        check(f"网关清单含 {method.upper():<6} {pattern}",
              (method, pattern) in gw_set,
              f"网关少一条 → 那条路由在网关上敞着（后端仍会拦，但粗筛失效）。"
              f"网关现有：{sorted(gw_set)}")
    check(f"网关清单恰好 {len(expected_gw)} 条（不多不少）",
          len(gw_entries) == len(expected_gw),
          f"实际 {len(gw_entries)} 条：{sorted(gw_set)}")

    # 双向：后端写路由条数与网关条数必须相等 ——
    # 只查「网关有的后端也有」不够，反向（后端加了网关没加）才是 14b 的隐患
    check("后端写路由数 == 网关 KB_WRITE_ROUTES 条数",
          len(write_routes) == len(gw_entries),
          f"后端 {len(write_routes)} 条 / 网关 {len(gw_entries)} 条 —— "
          f"新增写路由时两边都要改（见 14b 迭代文档 §6.3）")

# 2h 🔴 「不匹配被拒」的日志必须在 raise 之前 —— 14e 顺手修的真 bug
# ---------------------------------------------------------------------------
# 这条与知识库权限没有直接关系，是 14e 真链路探测时从后端日志里看出来的：
#     token_version 不匹配（token 已失效）| uid=wu.jing 持有=3 库里=3
# 两边都是 3、请求照常放行，却打了一句「已失效」。原因是那条 warn 写在
# `raise` 的**下一行** —— Python 会先抛异常，那行永远执行不到。
#
# 为什么值得单独立一条断言：
#   · 一条与事实相反的日志比没有日志更糟 —— 排障时会先信它，
#     于是往「token 版本对不上」这个方向查半天，而真问题在别处；
#   · 它是**静默**的：功能测试全绿、接口 200、没有任何报错；
#   · 判据不是「日志内容对不对」而是「**warn 在 raise 之前还是之后**」，
#     所以只能扫源码顺序 —— 跑一百遍也测不出来。
print("\n-- 2h 不匹配被拒的 warn 必须写在 raise 之前 --")
IDENTITY_PY = REPO_ROOT / "core" / "identity.py"
id_lines = IDENTITY_PY.read_text(encoding="utf-8").split("\n")
warn_i = raise_i = None
for i, line in enumerate(id_lines):
    if "token_version 不匹配（token 已失效）" in line:
        warn_i = i
    # 该分支里的 raise（401 失效提示）
    if "登录状态已失效（密码或权限已变更）" in line and raise_i is None and warn_i is not None:
        raise_i = i
check("找得到那条 warn", warn_i is not None, "identity.py 里没有这条日志了")
check("找得到那条 raise", raise_i is not None, "identity.py 里没有这条 401 了")
if warn_i is not None and raise_i is not None:
    check("warn 排在 raise 之前（否则 warn 永远执行不到）", warn_i < raise_i,
          f"warn 在第 {warn_i + 1} 行、raise 在第 {raise_i + 1} 行 —— "
          f"warn 写在 raise 之后是死代码，日志会与事实相反")


# --------------------------------------------------------------------------- #
# 收尾
# --------------------------------------------------------------------------- #
print("\n== 收尾：本模块不写库 ==")
print("  本模块只构造内存里的 Actor 对象、调纯函数、读源码 ——")
print("  不碰 MySQL / Chroma / upload/，因此没有要清的行。")

# 护栏自查：本模块不该改变知识库规模
try:
    import subprocess
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "kb_guard.py"), "--count-only"],
        capture_output=True, text=True, timeout=60,
    )
    tail = (out.stdout or "").strip().split("\n")[-2:]
    print(f"  护栏计数：{tail}")
    check("知识库规模未变（35/ 35）",
          any("35" in l for l in tail), f"实际：{tail}")
except Exception as e:  # noqa: BLE001
    #护栏跑不起来不算测试失败（它依赖中间件），但必须让人看见
    print(f"  ⚠️ 护栏计数没跑成（不影响本模块结论）：{e}")

print()
print("=" * 70)
print(f"P2-14e 结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)