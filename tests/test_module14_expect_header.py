# pyright: basic
"""
P2-14f 测试：网关身份头注入的落点（`Expect: 100-continue` 那条静默失效）。

    .venv/bin/python tests/test_module14_expect_header.py

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
`gateway/scripts/probe-expect-header-drop.cjs` 的结论必须一直成立：

    旧写法（剥离/注入在 on.proxyReq 里）+ 客户端带 Expect → 后端收不到身份头
    新写法（转发前盖入站 req.headers）                  → 后端一定收得到

以及「客户端伪造的身份头在任何情况下都到不了后端」。

--------------------------------------------------------------------------
为什么这个缺陷能活这么久，以及为什么常规测试抓不到它
--------------------------------------------------------------------------
两个条件必须**同时**满足才会踩到：

    ① 请求带了 `Expect: 100-continue`
    ② 客户端是 curl / 部分 HTTP 客户端 / 压测工具

浏览器 `fetch`/`XHR` 默认**不发** `Expect`，所以「页面上点上传」永远走不到这条路；
12b 当年量化伪造头穿透时用的是浏览器式请求（不带 Expect），也走不到。
两条路各自都没错，**只有它们交叉的那一个格子是洞**，而没人会去测那一个格子。

更糟的是它**完全静默**：接口返回 200、不报错、功能测试全绿。
唯一的症状是「后端日志里 actor=admin uid=None source=breakglass」——
而 break-glass 超管在 dev 模式下本来就是合法身份，所以连日志都不刺眼。

--------------------------------------------------------------------------
所以本模块的判据分两层（缺一层都不够）
--------------------------------------------------------------------------
第 1 组 **真链路**：直接跑 `probe-expect-header-drop.cjs`，读它的退出码。
    它用真实的 `http-proxy-middleware@3.0.7` + `http-proxy@1.18.1` 起服务，
    真的发请求、真的抓下游收到的头。这层能抓住「库换了实现」「修法失效」。

第 2 组 **源码结构**：断言剥离/注入**不在** `on.proxyReq` 回调里、
    且在两个转发 handler 里都被调用。
    这层能抓住「有人把代码挪回去了」—— 那正是 14e 反向验证第 6 条的形状
    （新增一条没挂守卫的路由，矩阵断言看不见它）。

只有第 1 组不够：有人可能注释掉脚本里的某个 case 让它变绿。
只有第 2 组不够：源码写着「在入站盖章」不代表真的在转发之前调用了。
两组交叉才有意义 —— 和 module14 既有做法同源。

--------------------------------------------------------------------------
写操作与库
--------------------------------------------------------------------------
本模块**不写库、不写文件**（只读 `proxy.controller.ts` 的源码 + 跑一个只监听
随机端口的 node 子进程，跑完即退）。种子员工与审计表都不受影响。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GW = ROOT / "gateway"
PROXY_TS = GW / "src" / "proxy" / "proxy.controller.ts"
PROBE_JS = GW / "scripts" / "probe-expect-header-drop.cjs"

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


# ⚠️ `PROXY_SRC` 是**源码文本**（不是 Path）—— 反向验证组要拿它跟磁盘上的
# 原文件逐字节比对还原。历史上这里曾把 Path 变量复用到文本变量上，
# 于是 `PROXY_SRC.read_text()` 报「str 没有 read_text」——
# 教训与踩坑清单第 52 条同源：一个名字只该指一种东西。
PROXY_SRC = PROXY_TS.read_text(encoding="utf-8")

# --------------------------------------------------------------------------- #
# 第 1 组：真链路 —— 跑 node 复现脚本
# --------------------------------------------------------------------------- #
section("第 1 组：真链路（真实 http-proxy-middleware，跑完即退）")

check("复现脚本在（它是第 1 组的唯一判据来源）", PROBE_JS.exists(), str(PROBE_JS))
check("网关依赖已安装（跑脚本的前提）",
      (GW / "node_modules" / "http-proxy-middleware" / "package.json").exists(),
      "gateway/node_modules 不存在 —— 先 cd gateway && npm install")

# ⚠️ node 缺失时**必须红**，不能静默跳过。
# 「跑不起来就当过了」是所有验收脚本里最常见的一种自欺：
# 它让 CI 全绿，而要防的那类缺陷（静默失效）恰恰只在真链路上现形。
# 提示写得具体一点，因为本仓库的 make test 并不预设 node 在 PATH 里。
_node_ok = True
try:
    subprocess.run(["node", "--version"], capture_output=True, timeout=20, check=True)
except Exception as e:  # noqa: BLE001
    _node_ok = False
    _node_err = f"{type(e).__name__}: {e}"
check("node 可用（第 1 组要真跑 http-proxy-middleware，缺它就验不了）", _node_ok,
      locals().get("_node_err", "")
      + "｜装法：brew install node，或临时 export PATH=<node bin> 目录再跑本测试")

if PROBE_JS.exists() and _node_ok:
    node = "node"
    r = subprocess.run(
        [node, str(PROBE_JS)],
        cwd=str(GW),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = r.stdout
    # 把脚本自己的 8 条结论逐条读出来，而不是只看退出码 ——
    # 退出码只说「有失败」，不说「哪条失败」。排障时后者才是有用的。
    script_results = {}
    for line in out.splitlines():
        m = re.match(r"^(PASS|FAIL) \| (.+)$", line.strip())
        if m:
            script_results[m.group(2)] = m.group(1)
    for line in out.splitlines():
        if "身份头在" in line or "身份头丢失" in line or re.match(r"^(PASS|FAIL) \|", line.strip()):
            print(f"    {line}")

    expected = [
        "A 基线必须有身份头",
        "B multipart 本身不影响（此前「multipart 丢头」是误判）",
        "C 旧写法 + 带 Expect → 身份头全丢（缺陷本体仍复现）",
        "D 新写法 + 带 Expect → 身份头在（这就是修法）",
        "E 新写法 + 无 Expect → 身份头在（没把正常路径弄坏）",
        "F 只摘 expect 也能恢复（证明根因确实是 Expect）",
        "G 旧写法 + 伪造头 + 带 Expect → 伪造身份真的穿透了（这就是 12b 那条洞的新入口）",
        "H 新写法 + 伪造头 + 带 Expect → 下游看到的是真身份（洞被关上）",
    ]
    for name in expected:
        # 结论缺失也要红 ——「脚本没跑出来这一条」和「这条不成立」同样是不能放过的事
        check(f"真链路 · {name}", script_results.get(name) == "PASS",
              f"脚本里这条是 {script_results.get(name) or '缺失（没跑出来）'}")
    check("真链路 · 子进程退出码为 0", r.returncode == 0,
          f"exit={r.returncode} stderr={(r.stderr or '')[-400:]}")
    check("真链路 · 脚本确实跑起来了（不是静默跳过）",
          bool(script_results), f"stdout 为空：{out[:200]}")

# --------------------------------------------------------------------------- #
# 第 2 组：源码结构 —— 代码不许挪回 on.proxyReq
# --------------------------------------------------------------------------- #
section("第 2 组：源码结构（注入必须落在转发之前，不在 on.proxyReq 里）")

# 切出 on.proxyReq 回调体。按大括号配对切，不用正则匹配到下一个 `},`
# —— 那会在嵌套对象/模板字符串上切错位置（踩坑清单第 52 条的形状）。
_m = re.search(r"proxyReq:\s*\(proxyReq,\s*req\)\s*=>\s*\{", PROXY_SRC)
check("找到 on.proxyReq 回调（判据的前提）", _m is not None)
proxy_req_body = ""
if _m:
    i = PROXY_SRC.index("{", _m.end() - 1)
    depth = 0
    for j in range(i, len(PROXY_SRC)):
        if PROXY_SRC[j] == "{":
            depth += 1
        elif PROXY_SRC[j] == "}":
            depth -= 1
            if depth == 0:
                proxy_req_body = PROXY_SRC[i : j + 1]
                break
    check("on.proxyReq 回调体切出来了（不能为空）", len(proxy_req_body) > 50,
          f"长度={len(proxy_req_body)}")

if proxy_req_body:
    check("🔴 on.proxyReq 里**没有**身份头剥离（INBOUND_IDENTITY_HEADERS 不该出现在这）",
          "INBOUND_IDENTITY_HEADERS" not in proxy_req_body,
          "它出现在 on.proxyReq 里 = 注入又挪回那个「Expect 时不触发」的回调了")
    check("🔴 on.proxyReq 里**没有**调用 buildForwardIdentityHeaders",
          "buildForwardIdentityHeaders" not in proxy_req_body,
          "同上")
    check("🔴 on.proxyReq 里没有裸的 removeHeader/setHeader 注入身份（只该留 Accept-Encoding）",
          not re.search(r"removeHeader\(", proxy_req_body),
          "on.proxyReq 里还有 removeHeader —— 剥离又挪回去了")
    check("on.proxyReq 仍然设置 Accept-Encoding: identity（NDJSON 不缓冲的前提）",
          "Accept-Encoding" in proxy_req_body)
    check("on.proxyReq 仍然调用 fixRequestBody（body-parser 吃过的 body 要重写）",
          "fixRequestBody" in proxy_req_body)
    # 顺序：Accept-Encoding 必须在 fixRequestBody 之前（后者会 end 掉 proxyReq）
    ai = proxy_req_body.find("Accept-Encoding")
    fi = proxy_req_body.find("fixRequestBody")
    check("Accept-Encoding 排在 fixRequestBody 之前（否则 headers-sent）",
          0 <= ai < fi, f"Accept-Encoding@{ai} fixRequestBody@{fi}")

# --- 盖章方法本身 ---
check("定义了 stampIdentityOnInbound（入站盖章的落点）",
      "stampIdentityOnInbound" in PROXY_SRC)
_m2 = re.search(r"private stampIdentityOnInbound\(req: ProxiedRequest\): void \{", PROXY_SRC)
check("stampIdentityOnInbound 的签名可定位（判据的前提）", _m2 is not None)

stamp_body = ""
if _m2:
    i = PROXY_SRC.index("{", _m2.end() - 1)
    depth = 0
    for j in range(i, len(PROXY_SRC)):
        if PROXY_SRC[j] == "{":
            depth += 1
        elif PROXY_SRC[j] == "}":
            depth -= 1
            if depth == 0:
                stamp_body = PROXY_SRC[i : j + 1]
                break
    check("stampIdentityOnInbound 方法体切出来了", len(stamp_body) > 100,
          f"长度={len(stamp_body)}")

if stamp_body:
    check("stamp 里做无条件剥离（遍历 INBOUND_IDENTITY_HEADERS）",
          "INBOUND_IDENTITY_HEADERS" in stamp_body and "delete req.headers[h]" in stamp_body,
          "剥离不见了")
    # 剥离不能挂在任何 if 里 —— 12b 的核心教训
    check("🔴 剥离**不挂在任何 if 里**（条件式剥离 = 白名单路径可伪造）",
          not re.search(r"if\s*\([^)]*\)\s*\{?\s*for \(const h of INBOUND_IDENTITY_HEADERS\)",
                        stamp_body)
          and not re.search(r"for \(const h of INBOUND_IDENTITY_HEADERS\)\s*\{\s*if",
                            stamp_body),
          "剥离被包进了 if")
    check("stamp 里调用 buildForwardIdentityHeaders（真身份从守卫结果来）",
          "buildForwardIdentityHeaders" in stamp_body)
    check("stamp 里注入前用了 req.user（而不是 req.headers['x-user-id']）",
          "if (req.user)" in stamp_body,
          "没从守卫结果取身份")
    check("stamp 里写了入站头（req.headers[...] = v）",
          "req.headers[k.toLowerCase()] = v" in stamp_body
          or re.search(r"req\.headers\[[^\]]+\]\s*=", stamp_body) is not None)
    check("🔴 stamp 里摘掉 expect（否则 fixRequestBody / Accept-Encoding 失效）",
          "delete req.headers.expect" in stamp_body)
    # 剥离必须排在注入之前
    si = stamp_body.find("delete req.headers[h]")
    ii = stamp_body.find("buildForwardIdentityHeaders")
    check("剥离排在注入之前（否则真身份被自己剥掉 → 全站 401）",
          0 <= si < ii, f"剥离@{si} 注入@{ii}")

# --- 两个转发 handler 都要调用 ---
check("白名单 handler（handlePublicHealth）调用了 stampIdentityOnInbound",
      re.search(r"handlePublicHealth[\s\S]{0,400}stampIdentityOnInbound", PROXY_SRC) is not None,
      "白名单路径没盖章 —— 探活接口在后端会全被 401")
check("通配 handler（handle）调用了 stampIdentityOnInbound",
      re.search(r"@All\('api/v1/\*'\)[\s\S]{0,2000}stampIdentityOnInbound", PROXY_SRC) is not None,
      "通配路由没盖章 —— 所有数据接口都会拿不到身份")
# 调用必须排在 this.proxy 之前
check("盖章排在 this.proxy(...) 之前（之后请求已经发出去了）",
      all(
          stamp_body_i != -1 and proxy_i != -1 and stamp_body_i < proxy_i
          for stamp_body_i, proxy_i in [
              (m.start(), PROXY_SRC.find("this.proxy(req, res, next)", m.start()))
              for m in re.finditer(r"stampIdentityOnInbound\(req\);", PROXY_SRC)
          ]
      ),
      "有调用点排在 this.proxy 之后")

# --------------------------------------------------------------------------- #
# 第 3 组：反向验证（拆掉修法，断言必须转红）
# --------------------------------------------------------------------------- #
section("第 3 组：反向验证（--reverse 才跑：会临时改生产代码）")

if "--reverse" in sys.argv:
    backup = PROXY_SRC
    try:
        # 反向 1：把盖章挪回 on.proxyReq（还原 14f 之前的写法）→ 第 2 组必须红
        broken = backup.replace("    this.stampIdentityOnInbound(req);\n", "")
        check("反向验证：把盖章调用删掉后，源码里确实找不到了（所以第 2 组的 handler 断言有判别力）",
              "stampIdentityOnInbound(req);" not in broken
              and backup.count("stampIdentityOnInbound(req);") == 2,
              f"原文件里调用次数={backup.count('stampIdentityOnInbound(req);')}")

        # 反向 2：把剥离包进 if → 「剥离不挂 if」那条必须红
        re1 = "for (const h of INBOUND_IDENTITY_HEADERS) {\n      delete req.headers[h];\n    }"
        check("反向验证：剥离那段在源码里是可定位的（前提）", re1 in backup)
        broken2 = backup.replace(
            re1,
            "if (req.user) {\n      for (const h of INBOUND_IDENTITY_HEADERS) {\n        delete req.headers[h];\n      }\n    }",
        )
        check("反向验证：把剥离包进 if 之后，「无条件」判据能检测到",
              broken2 != backup
              and re.search(r"if\s*\([^)]*\)\s*\{?\s*for \(const h of INBOUND_IDENTITY_HEADERS\)",
                            broken2) is not None,
              "条件式写法没被检测到 —— 判据本身失效了")

        # 反向 3：删掉摘 expect → 「摘 expect」那条必须红
        check("反向验证：摘 expect 那一行在源码里是可定位的（前提）",
              "delete req.headers.expect" in backup)
        broken3 = backup.replace("    delete req.headers.expect;\n", "")
        check("反向验证：删掉摘 expect 后，那条判据能检测到",
              "delete req.headers.expect" not in broken3)

        # 反向 4：把注入排到剥离之前 → 「顺序」那条必须红
        # ⚠️ 顺序必须在 **stamp_body 里**比，不能在整份源码里 find ——
        # 文件头的注释里就出现了 `buildForwardIdentityHeaders` 这个词，
        # 整份 find 命中的是注释（偏移 1844），不是真正的调用（偏移 8892），
        # 于是这条断言会恒假 —— 一个恒假的断言比没有断言更坏，它给人
        # 「已经验过了」的错觉（踩坑清单第 50 条的形状：不变量没验到不变量）。
        check("反向验证：顺序判据必须在 stamp_body 里比，不是整份源码（前提）",
              0 <= stamp_body.find("delete req.headers[h]")
              < stamp_body.find("buildForwardIdentityHeaders"),
              f"stamp 内剥离@{stamp_body.find('delete req.headers[h]')} "
              f"注入@{stamp_body.find('buildForwardIdentityHeaders')}")
        check("反向验证：当前源码里剥离确实排在注入之前（所以这条不是恒真）",
              stamp_body.find("delete req.headers[h]")
              < stamp_body.find("buildForwardIdentityHeaders"),
              f"stamp 内剥离@{stamp_body.find('delete req.headers[h]')} "
              f"注入@{stamp_body.find('buildForwardIdentityHeaders')}")
    finally:
        # 还原自查：文件必须与读进来时逐字节相同
        PROXY_TS.write_text(backup, encoding="utf-8")
        check("反向验证：生产代码已还原（逐字节比对）",
              PROXY_TS.read_text(encoding="utf-8") == backup)
else:
    print("  （反向验证组已跳过 —— 加 --reverse 参数执行；理由见该组注释）")

print("\n" + "=" * 66)
print(f"  P2-14f 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)