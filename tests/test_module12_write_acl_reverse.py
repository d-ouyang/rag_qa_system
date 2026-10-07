# pyright: basic
"""
P2-14e 反向验证 —— **真的把守卫拆掉，然后看断言转红**。

    .venv/bin/python tests/test_module12_write_acl_reverse.py

--------------------------------------------------------------------------
为什么单独一个文件（与 14b 反向验证同一取舍）
--------------------------------------------------------------------------
反向验证要**临时改生产代码**（`api/routes/documents.py`、
`core/identity.py`、`gateway/src/auth/path-authorization.ts`），
再重跑主测试看断言转红。

塞进主测试文件意味着：
    · 每次跑回归都在动源码，主测试的失败原因会变得不可判
      （「是回归真失败，还是反向验证把自己改坏了」）；
    · 中途异常退出会把生产代码留在**被拆掉的状态** ——
      而这次拆掉的东西是「知识库的写权限守卫」。

所以：独立文件 + 改完无条件还原 + 还原后自查+ 最后重跑主测试确认恢复。

--------------------------------------------------------------------------
「反向验证合格」的判据（本项目铁律）
--------------------------------------------------------------------------
不是「跑出 OK」，而是**断言必须转红**。

一份永远绿的「反向验证」只能证明它自己没报错，证明不了那道守卫真的在守。
所以本文件末尾会：
    ① 报出每条断言拆前 / 拆后的结果；
    ② 断言「拆掉之后那些断言**确实红了**」；
    ③ 还原源码并重跑主测试，确认恢复成 169/0。

⚠️ **崩掉与失败必须能区分**（14b 反向验证踩过）：
如果改法写坏了生产代码，主测试会**崩在 import**，一条断言都没跑到，
而本文件只看到「输出里找不到那条断言」—— 看起来像「断言没红」，
实际是「改法错了」。所以下面每一步都先确认**改完之后还能 import**。

--------------------------------------------------------------------------
七条反向验证分别拆什么
--------------------------------------------------------------------------
    1. 拆 `POST /upload` 的 `Depends(require_kb_upload)` → 2a/2b 必须转红
    2. 拆 `DELETE /{doc_id}` 的 `Depends(require_kb_delete)` → 2a/2b 必须转红
    3. 把 `require_kb` 里的拒权改成**放行**（模拟守卫写反）→ 第 3 组必须转红
    4. 把 `kb_acl.describe` 的参数名改回14a 那个 bug → 3组必须转红
       （这一条专门验「拒权路径本身能返回 403」那条断言真的有判别力）
    5. 从网关 `KB_WRITE_ROUTES` 删掉一条 → 2g 必须转红
       （验 14b 遗留的那条「两侧各写一份」现在被机械看住了）
    6. 加一条**没挂守卫**的新写路由 → 2a/2b/2c 必须转红
       （这一条才是 14e 存在的核心理由：14.0 说「漏一个路由就完全没有防护」，
       而矩阵断言对「已枚举之外的新路由」完全隐形）
    7. 把「不匹配被拒」的 warn 挪回 raise 之后 → 2h 必须转红
       （验 14e 顺手修掉的那个「日志与事实相反」的 bug 有断言看住）
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOCUMENTS_PY = ROOT / "api" / "routes" / "documents.py"
IDENTITY_PY = ROOT / "core" / "identity.py"
KB_ACL_PY = ROOT / "core" / "kb_acl.py"
PATH_AUTH_TS = ROOT / "gateway" / "src" / "auth" / "path-authorization.ts"

MAIN_TEST = ROOT / "tests" / "test_module12_write_acl.py"
#: 主测试正常时的通过数。反向验证结束时要确认恢复成这个数。
BASELINE_PASS = 172

FAILURES: list[str] = []
PASSES: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    mark = "  [OK]  " if ok else "  [RED] "
    print(f"{mark}{name}" + (f" | {detail}" if detail else ""))


def run_main() -> tuple[int, str, bool]:
    """跑主测试。返回 (通过数, 输出, 是否崩在import/语法层)。"""
    out = subprocess.run(
        [sys.executable, str(MAIN_TEST)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    combined = out.stdout + out.stderr
    m = re.search(r"P2-14e 结果：(\d+) 通过 / (\d+) 失败", combined)
    passed = int(m.group(1)) if m else 0
    # 崩掉的现象：连「结果」那行都没有（import 失败 / 语法错）
    crashed = m is None
    return passed, combined, crashed


def red_lines_containing(output: str, keyword: str) -> list[str]:
    return [ln for ln in output.splitlines() if keyword in ln and "[FAIL]" in ln]


def main() -> int:
    print("=" * 70)
    print("  P2-14e 反向验证：拆掉守卫，断言必须转红")
    print("=" * 70)

    # ---------- 备份（改之前一定先存一份，finally 里还原） ----------
    tmp = Path(tempfile.mkdtemp(prefix="p2-14e-reverse-"))
    targets = (DOCUMENTS_PY, IDENTITY_PY, KB_ACL_PY, PATH_AUTH_TS)
    originals: dict[Path, str] = {}
    for f in targets:
        b = tmp / f.name
        shutil.copy2(f, b)
        originals[f] = b.read_text(encoding="utf-8")

    def restore() -> None:
        for f, text in originals.items():
            f.write_text(text, encoding="utf-8")

    try:
        # ================================================================
        print("\n--- 基线：先确认主测试是绿的---")
        passed, out, crashed = run_main()
        if crashed:
            report("基线主测试能跑完（没崩在 import）", False, out[-500:])
            print("\n基线就不对，反向验证无从谈起。")
            return 1
        report(f"基线主测试通过数 == {BASELINE_PASS}", passed == BASELINE_PASS,
               f"实际 {passed}")

        # ================================================================
        print("\n--- 反向验证 1：拆掉 POST /upload 的守卫 ---")
        # ================================================================
        # 拆法：把 `actor: Actor = Depends(require_kb_upload),` 从
        # `upload_document` 的签名里删掉。签名里那一行是唯一的。
        src = originals[DOCUMENTS_PY]
        patched, n = re.subn(
            r"\n\s*actor: Actor = Depends\(require_kb_upload\),\n(\s*\) -> dict\[str, Any\]:\n"
            r"\s*\"\"\".*?\"\"\"\n)",
            r"\n\1",
            src,
            count=1,
            flags=re.S,
        )
        if n == 0:
            # 兜底：只删第一处出现（上传路由在文件最前面）
            patched, n = re.subn(
                r"\n\s*actor: Actor = Depends\(require_kb_upload\),", "", src, count=1
            )
        if patched == src:
            report("反向验证 1 的拆法生效（删掉了上传路由的守卫依赖）", False,
                   "没匹配到 —— upload_document 签名变了，这里要同步")
        else:
            report("反向验证 1 的拆法生效（删掉了上传路由的守卫依赖）", True)
            DOCUMENTS_PY.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                if crashed:
                    report("⚠️ 拆掉之后主测试**崩了**（改法坏了，不是断言红）", False,
                           out[-400:])
                else:
                    reds = red_lines_containing(out, "L100 POST   /upload")
                    report("⚠️ 拆掉之后「2a 写路由挂了 kb 守卫」那条**转红**",
                           bool(reds), reds[0].strip() if reds else "（没找到那条断言）")
                    reds2 = red_lines_containing(out, "挂 require_kb_upload")
                    report("⚠️ 拆掉之后「2b 守卫与语义对上」那条**转红**",
                           bool(reds2), reds2[0].strip() if reds2 else "（没找到）")
                    report("⚠️ 拆掉之后有**多条**断言转红（不是只红一条）",
                           out.count("[FAIL]") >= 2, f"{out.count('[FAIL]')} 条转红")
            finally:
                restore()

        # ================================================================
        print("\n--- 反向验证 2：拆掉 DELETE /{doc_id} 的守卫 ---")
        # ================================================================
        # 与 1 刻意选**不同**的路由：证明断言不是只对第一条生效。
        src = originals[DOCUMENTS_PY]
        patched, n = re.subn(
            r"\n\s*actor: Actor = Depends\(require_kb_delete\),", "", src, count=1
        )
        if patched == src:
            report("反向验证 2 的拆法生效（删掉了删除路由的守卫依赖）", False,
                   "没匹配到 —— delete_document 签名变了，这里要同步")
        else:
            report("反向验证 2 的拆法生效（删掉了删除路由的守卫依赖）", True)
            DOCUMENTS_PY.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                if crashed:
                    report("⚠️ 拆掉之后主测试**崩了**", False, out[-400:])
                else:
                    reds = red_lines_containing(out, "L485 DELETE /{doc_id}")
                    report("⚠️ 拆掉之后「2a 删除路由挂了 kb 守卫」那条**转红**",
                           bool(reds), reds[0].strip() if reds else "（没找到）")
                    reds2 = red_lines_containing(out, "挂 require_kb_delete")
                    report("⚠️ 拆掉之后「2b 删除挂 require_kb_delete」那条**转红**",
                           bool(reds2), reds2[0].strip() if reds2 else "（没找到）")
                    # 最关键的一条：写路由条数不变（还是 4 条），
                    # 所以 2c「条数与 14a 认定一致」**不该红** ——
                    # 它守的是「多出来」，不是「少了依赖」。这条不该红正是对的。
                    reds3 = red_lines_containing(out, "写路由共 4 条")
                    report("（对照）2c「写路由条数」那条**仍然绿**（它只管「多出来」）",
                           not reds3,
                           reds3[0].strip() if reds3 else "")
            finally:
                restore()

        # ================================================================
        print("\n--- 反向验证 3：把守卫写反（拒权改成放行）---")
        # ================================================================
        # 拆法：`if not kb_acl.can(...)` → `if False and not kb_acl.can(...)`。
        # 等价于「这道门永远开着」—— 也就是「所有登录用户都能改知识库」，
        # 也就是 14.0 实测的那个基线状态。
        src = originals[IDENTITY_PY]
        patched = src.replace(
            "if not kb_acl.can(actor.kb_role, action):",
            "if False and not kb_acl.can(actor.kb_role, action):",
            1,
        )
        if patched == src:
            report("反向验证 3 的拆法生效（守卫被改成永远放行）", False,
                   "没找到 `if not kb_acl.can(actor.kb_role, action):`")
        else:
            report("反向验证 3 的拆法生效（守卫被改成永远放行）", True)
            IDENTITY_PY.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                if crashed:
                    report("⚠️ 拆掉之后主测试**崩了**", False, out[-400:])
                else:
                    # 判据是「居然放行了」—— 守卫改成永远放行之后，
                    # 主测试第3 组走的是**「居然放行了」那个分支**，
                    # 而「拒权 → 403（不是 500）」那条压根不会被打印。
                    # ⚠️ 第一版按「→ 403」找红行，找不到就以为没抓到 ——
                    # 实际是判据选错了：4 条断言确实红了，只是红的理由不同。
                    # **教训与 14b 那条一致：断言转红之后要看它为什么红。**
                    reds = red_lines_containing(out, "居然放行了")
                    report("⚠️ 守卫放行后「拒绝某档」那组**转红**",
                           bool(reds), reds[0].strip() if reds else "（没找到）")
                    reds2 = red_lines_containing(out, "竟抛了")
                    report("⚠️ 守卫放行后「有权限那组放行」不该被误判成红（它确实绿）",
                           not reds2,
                           reds2[0].strip() if reds2 else "")
                    report("⚠️ 守卫放行后有**多条**断言转红（不是只红一条）",
                           out.count("[FAIL]") >= 3, f"{out.count('[FAIL]')} 条转红")
            finally:
                restore()

        # ================================================================
        print("\n--- 反向验证 4：把 describe 的参数名改回 14a 那个 bug ---")
        # ================================================================
        # 这一条专门验「拒权路径本身要能正常返回 403」那条断言**真的有判别力**。
        # 还原 14a 的 bug：签名写 `kb_role`，函数体用 `action` → NameError → 500。
        # 主测试第 3 组是直接调依赖函数的，会当场抛 NameError 而不是 HTTPException。
        src = originals[KB_ACL_PY]
        patched = src.replace(
            "def describe(action: str) -> str:",
            "def describe(kb_role: str) -> str:",
            1,
        )
        if patched == src:
            report("反向验证 4 的拆法生效（describe 参数名被改回 bug 版）", False,
                   "没找到 `def describe(action: str) -> str:`")
        else:
            report("反向验证 4 的拆法生效（describe 参数名被改回 bug 版）", True)
            KB_ACL_PY.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                # 这个 bug 的特点是**崩在调用处**（NameError 不是 HTTPException），
                # 所以主测试会异常退出 —— 崩在这里本身就是「断言抓到了」。
                has_traceback = "NameError" in out
                report("⚠️ 还原 14a 那个 bug 后出现 NameError（拒权路径不再是 403）",
                       has_traceback,
                       "没看到 NameError —— 那条断言对「拒权路径本身」没有判别力")
                report("（对照）这次是**崩**而不是「断言红」，也算抓到",
                       True, "崩掉本身就是证据：14a 那个 bug 的真实症状")
            finally:
                restore()

        # ================================================================
        print("\n--- 反向验证 5：网关清单删掉一条 ---")
        # ================================================================
        # 14b 的遗留项：「后端四条写路由」与「网关 KB_WRITE_ROUTES」
        # 各写一份、靠人同步。删掉网关那一条 → 2g 必须转红。
        src = originals[PATH_AUTH_TS]
        patched = re.sub(
            r"\n\s*\{ method: 'DELETE', pattern: /\^\\\/api\\\/v1\\\/documents\\\/\\d\+\$/ \},",
            "", src, count=1,
        )
        if patched == src:
            report("反向验证 5 的拆法生效（网关清单里能删掉 DELETE 那条）", False,
                   "没匹配到 —— 网关清单格式变了，这里要同步")
        else:
            report("反向验证 5 的拆法生效（网关清单里能删掉了 DELETE 那条）", True)
            PATH_AUTH_TS.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                if crashed:
                    report("⚠️ 拆掉之后主测试**崩了**", False, out[-400:])
                else:
                    reds = red_lines_containing(out, "网关清单恰好")
                    report("⚠️ 网关少一条后「2g 恰好 N 条」那条**转红**",
                           bool(reds), reds[0].strip() if reds else "（没找到）")
                    reds2 = red_lines_containing(out, "网关清单含 DELETE")
                    report("⚠️ 网关少一条后「2g 含 DELETE 那条」**转红**",
                           bool(reds2), reds2[0].strip() if reds2 else "（没找到）")
                    reds3 = red_lines_containing(out, "后端写路由数 == 网关")
                    report("⚠️ 网关少一条后「两侧条数相等」那条**转红**",
                           bool(reds3), reds3[0].strip() if reds3 else "（没找到）")
            finally:
                restore()

        # ================================================================
        print("\n--- 新增写路由但忘挂守卫（模拟 14.0 说的那个洞）---")
        # ================================================================
        # 这一条是 14e 存在的**核心理由**：14.0 说「漏一个路由就完全没有防护」。
        # 模拟方式：在 documents.py 里加一条**没挂守卫**的写路由，
        # 看结构断言能不能抓到它。
        #
        # ⚠️ 抓到的判据是「2b 那条：不在 14a 枚举里的写路由」——
        # 2a 也会红（因为它没有 require_kb_*）。两条都红才算抓到。
        src = originals[DOCUMENTS_PY]
        new_route = '''

@router.post(
    "/import-from-external",
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="从外部系统导入（模拟：新增但忘挂守卫的写路由）",
)
def import_from_external() -> dict[str, Any]:
    """故意不挂 require_kb_* —— 这一条路由就是 14.0 说的「漏一个就完全没有防护」。"""
    return {"ok": True}

'''
        patched = src + new_route
        DOCUMENTS_PY.write_text(patched, encoding="utf-8")
        try:
            passed, out, crashed = run_main()
            if crashed:
                report("加了一条裸写路由后主测试**崩了**（改法坏了）", False, out[-400:])
            else:
                reds = red_lines_containing(out, "import-from-external")
                report("⚠️ 新增裸写路由后「2b 不在枚举内」那条**转红**",
                       bool(reds), reds[0].strip() if reds else "（没找到）")
                reds2 = red_lines_containing(out, "写路由共 4 条")
                report("⚠️ 新增裸写路由后「2c 条数与认定一致」**转红**",
                       bool(reds2), reds2[0].strip() if reds2 else "（没找到）")
                reds3 = [ln for ln in out.splitlines()
                         if "依赖含 require_kb_*" in ln and "[FAIL]" in ln]
                report("⚠️ 新增裸写路由后「2a 挂了 kb 守卫」**转红**",
                       bool(reds3), reds3[0].strip() if reds3 else "（没找到）")
        finally:
            restore()

    # ================================================================
        print("\n--- 反向验证 6：把 warn 挪回 raise 之后（还原 14e 修掉的那个 bug）---")
        # ================================================================
        # 这一条验的是 2h 那条断言真的有判别力。
        # 那个 bug 的形状特别：**功能测试全绿、接口 200、没有报错**，
        # 只有日志在说「token 已失效」，而实际上没失效。
        src = originals[IDENTITY_PY]
        # 拆法：把 warn 整块搬到 raise 之后（= 死代码，永远执行不到）
        warn_block = '''                logger.warning(
                    "token_version 不匹配（token 已失效）| uid=%s 持有=%s 库里=%s",
                    actor.record.username, claimed, actor.record.token_version,
                )
'''
        raise_block = '''                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="登录状态已失效（密码或权限已变更），请重新登录",
                )
'''
        if warn_block not in src or raise_block not in src:
            report("反向验证 6 的拆法生效", False,
                   "没找到 warn / raise 那一段 —— identity.py 变了，这里要同步")
        else:
            patched = src.replace(warn_block, "", 1).replace(
                raise_block, raise_block + warn_block, 1
            )
            report("反向验证 6 的拆法生效（warn 被挪到 raise 之后）", True)
            IDENTITY_PY.write_text(patched, encoding="utf-8")
            try:
                passed, out, crashed = run_main()
                if crashed:
                    report("⚠️ 改坏之后主测试**崩了**（改法坏了）", False, out[-400:])
                else:
                    reds = red_lines_containing(out, "warn 排在 raise 之前")
                    report("⚠️ warn 挪到 raise 之后后「2h」那条**转红**",
                           bool(reds), reds[0].strip() if reds else "（没找到）")
            finally:
                restore()

    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)

    # ================================================================ #
    # 还原自查
    # ================================================================ #
    print("\n--- 还原自查 ---")
    changed = []
    for f, text in originals.items():
        if f.read_text(encoding="utf-8") != text:
            changed.append(f.name)
    report("四个被改过的文件都已还原成原样", not changed, f"仍有差异：{changed}")

    passed, out, crashed = run_main()
    if crashed:
        report("还原后主测试能跑完", False, out[-400:])
    else:
        report(f"还原后主测试恢复成 {BASELINE_PASS} 通过",
               passed == BASELINE_PASS, f"实际 {passed}")
        m = re.search(r"P2-14e 结果：\d+ 通过 / (\d+) 失败", out)
        report("还原后 0 失败", (m and m.group(1) == "0"), "")

    # ================================================================ #
    print()
    print("=" * 70)
    print(f"  反向验证：{len(PASSES)} 条成立 / {len(FAILURES)} 条不成立")
    if FAILURES:
        print("  不成立的：")
        for f in FAILURES:
            print(f"    · {f}")
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())