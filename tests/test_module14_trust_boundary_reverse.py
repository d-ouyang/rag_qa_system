# pyright: basic
"""
P2-12b 反向验证 —— **真的把守卫拆掉，然后看断言转红**。

    .venv/bin/python tests/test_module14_trust_boundary_reverse.py

--------------------------------------------------------------------------
为什么单独一个文件，而不是 `test_module14_trust_boundary.py --reverse`
--------------------------------------------------------------------------
因为反向验证要**临时改生产代码**（Python 函数与 .ts 源码），再重跑对应断言。
把它塞进主测试文件意味着：
    · 每次跑回归都在动源码，而主测试的失败原因会变得不可判
      （「是回归真失败，还是反向验证把自己改坏了」）；
    · 中途异常退出会把生产代码留在被拆掉的状态。
所以：独立文件 + 改完无条件还原 + 还原后自查 + 最后重跑主测试确认恢复。

--------------------------------------------------------------------------
「反向验证合格」的判据（本项目铁律）
--------------------------------------------------------------------------
不是「跑出PASS」，而是**断言必须转红**。
一份永远绿的「反向验证」只能证明它自己没报错，证明不了那道守卫真的在守。
所以本文件末尾会：① 报出每条断言拆前/ 拆后的结果；
② 断言「拆掉之后那些断言**确实红了**」；
③ 还原源码并重跑一遍确认恢复成绿。任何一步不符就退出码非 0。
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os_env_backup = None

PROXY_TS = ROOT / "gateway" / "src" / "proxy" / "proxy.controller.ts"
HEADERS_TS = ROOT / "gateway" / "src" / "auth" / "identity-headers.ts"
IDENTITY_PY = ROOT / "core" / "identity.py"

FAILURES: list[str] = []
PASSES: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    mark = "  [OK]  " if ok else "  [RED] "
    print(f"{mark}{name}" + (f" | {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("  P2-12b 反向验证：拆掉守卫，断言必须转红")
    print("=" * 70)

    # ---------- 备份（改之前一定先存一份，finally 里还原） ----------
    tmp = Path(tempfile.mkdtemp(prefix="p2-12b-reverse-"))
    backups: dict[Path, Path] = {}
    for f in (PROXY_TS, HEADERS_TS, IDENTITY_PY):
        b = tmp / f.name
        shutil.copy2(f, b)
        backups[f] = b
    originals = {f: backups[f].read_text(encoding="utf-8") for f in backups}

    try:
        # ================================================================
        print("\n--- 反向验证 1：拆掉后端「验网关证明」---")
        # ================================================================
        # 拆法：把 resolve_actor 里那段 verify_gateway_proof(...) 调用删掉。
        # 拆完必须能重新 import，且伪造身份头 + 无证明**真的**过了 ——
        # 那就说明主测试第 5.2 组那条断言在正常情况下是靠这道校验撑着的。
        src = originals[IDENTITY_PY]
        patched = src.replace(
            "    if mode == MODE_GATEWAY:\n        verify_gateway_proof(gateway_proof_header)\n",
            "    # [反向验证] 网关证明校验已被拆掉\n",
            1,
        )
        if patched == src:
            report("反向验证 1 的拆法生效（真的删掉了证明校验调用）", False,
                   "没找到目标代码 —— 12b 改过 resolve_actor 的话这里要同步")
        else:
            report("反向验证 1 的拆法生效（真的删掉了证明校验调用）", True)
            IDENTITY_PY.write_text(patched, encoding="utf-8")
            try:
                out = subprocess.run(
                    [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
                    capture_output=True, text=True, cwd=str(ROOT),
                )
                # 主测试的 5.2 必须转红
                red_line = [
                    ln for ln in out.stdout.splitlines()
                    if "伪造 X-User-Id 且无网关证明" in ln
                ]
                turned_red = bool(red_line) and "[FAIL]" in red_line[0]
                report("⚠️ 拆掉之后，主测试「伪造身份头且无证明 → 401」那条**转红**",
                       turned_red, red_line[0].strip() if red_line else "（那条断言根本没跑到）")
                # 而且不能只红一条 —— 说明拆的是真防线，不是某条具体实现细节
                fail_count = out.stdout.count("[FAIL]")
                report("⚠️ 拆掉之后有**多条**断言转红（不是只红一条）",
                       fail_count >= 2, f"{fail_count} 条转红")
            finally:
                IDENTITY_PY.write_text(src, encoding="utf-8")

        # ================================================================
        print("\n--- 反向验证 2：把网关剥离改成条件式（还原 11c 的写法）---")
        # ================================================================
        # 拆法：给无条件剥离套上一个 `if (false)` ——等价于「没有剥离」。
        # 主测试第 3 组「剥离不在任何 if 内」必须转红。
        src = originals[PROXY_TS]
        patched = src.replace(
            "for (const h of INBOUND_IDENTITY_HEADERS) {",
            "if (incoming.user) for (const h of INBOUND_IDENTITY_HEADERS) {",
            1,
        )
        if patched == src:
            report("反向验证 2 的拆法生效", False, "没找到剥离那一段")
        else:
            report("反向验证 2 的拆法生效（剥离已被套进 if 里）", True)
            PROXY_TS.write_text(patched, encoding="utf-8")
            try:
                out = subprocess.run(
                    [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
                    capture_output=True, text=True, cwd=str(ROOT),
                )
                red_line = [
                    ln for ln in out.stdout.splitlines()
                    if "剥离不在任何 if 内" in ln
                ]
                turned_red = bool(red_line) and "[FAIL]" in red_line[0]
                report("⚠️ 条件式写法让「剥离不在任何 if 内」那条**转红**",
                       turned_red, red_line[0].strip() if red_line else "（那条断言根本没跑到）")
            finally:
                PROXY_TS.write_text(src, encoding="utf-8")

        # ================================================================
        print("\n--- 反向验证 3：调换剥离与注入的顺序---")
        # ================================================================
        # 拆法：把注入提到剥离前面（用交换两个代码块实现）。
        # 后果是「真身份被自己剥掉」→ 主测试第 3 组的顺序断言必须转红。
        src = originals[PROXY_TS]
        # ⚠️ 定位要按「剥离块 →注入块 → requestId 行」的真实顺序。
        # 第一版把 inject_end 写成 `find("if (incoming.requestId)", inject_start)`，
        # 而 inject_start 因为上面 inject_end 取的是同一个锚点、位置错乱而变成 -1，
        # 结果「拆法生效」那条自己先红了 —— 一个反向验证失败在**自己身上**。
        # 现在改成：从剥离块开始，一路找注入块的起点与终点。
        strip_block_start = src.find("            for (const h of INBOUND_IDENTITY_HEADERS) {")
        inject_start = src.find("            if (incoming.user) {")
        inject_end = src.find("            if (incoming.requestId)", inject_start)
        if -1 in (strip_block_start, inject_start, inject_end) or not (
            strip_block_start < inject_start < inject_end
        ):
            report("反向验证 3 的拆法生效（能定位到剥离块与注入块）", False,
                   f"strip={strip_block_start} inject=({inject_start},{inject_end})")
        else:
            strip_block = src[strip_block_start:inject_start]
            inject_block = src[inject_start:inject_end]
            patched = src[:strip_block_start] + inject_block + strip_block + src[inject_end:]
            report("反向验证 3 的拆法生效（注入被提到剥离之前）", True)
            PROXY_TS.write_text(patched, encoding="utf-8")
            try:
                out = subprocess.run(
                    [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
                    capture_output=True, text=True, cwd=str(ROOT),
                )
                red_line = [
                    ln for ln in out.stdout.splitlines() if "剥离排在注入" in ln
                ]
                turned_red = bool(red_line) and "[FAIL]" in red_line[0]
                report("⚠️ 顺序颠倒后「剥离排在注入之前」那条**转红**",
                       turned_red, red_line[0].strip() if red_line else "（那条断言根本没跑到）")
            finally:
                PROXY_TS.write_text(src, encoding="utf-8")

        # ================================================================
        print("\n--- 反向验证 4：从两侧清单里删掉一个头---")
        # ================================================================
        # 模拟 13d 那个坑的加强版：两侧清单**都**少一个，于是
        # 「两侧一致」那条断言仍然是绿的 —— 这正说明光有一致性断言不够，
        # 必须另有一条「清单必须等于期望值」的断言。这里验的正是它。
        #
        # ⚠️ 只删**清单字面量里**那一行，不能全局删 `'x-internal-auth'`：
        #   第一版全文替换，把 `GATEWAY_PROOF_HEADER = "x-internal-auth"`
        #   也删掉了 → SyntaxError → 主测试**崩在 import**，一条断言都没跑到，
        #   而反向验证只看到「输出里找不到那条断言」。
        #   **崩掉与失败必须能区分**：崩掉说明改法错了，不是断言红了。
        #   （这个坑与第 7 条同形 ——「一次通过不算通过」的反面：一次报错也不算验证。）
        for f, pattern in (
            (HEADERS_TS, r"(\n\s*['\"]x-internal-auth['\"],)"),
            (IDENTITY_PY, r"(\n\s*[\"']x-internal-auth[\"'],)"),
        ):
            src = originals[f]
            patched = re.sub(pattern, "", src)
            if patched == src:
                report(f"反向验证 4 的拆法生效（{f.suffix} 侧清单里能删掉 x-internal-auth）",
                       False, "没匹配到 —— 清单格式变了，这里要同步")
                continue
            f.write_text(patched, encoding="utf-8")

        try:
            out = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            crashed = "Traceback" in out.stderr or out.returncode not in (0, 1)
            report("反向验证 4 没有把主测试跑崩（崩了就是改法错，不是断言红）",
                   not crashed, out.stderr.strip()[-160:])
            red_line = [
                ln for ln in out.stdout.splitlines()
                if "清单里含 x-internal-auth" in ln and "[FAIL]" in ln
            ]
            report("⚠️ 两侧都删掉 x-internal-auth → 「清单含它」那条**转红**",
                   bool(red_line),
                   red_line[0].strip() if red_line
                   else "（没红 —— 说明只有一致性断言，没有「等于期望值」的断言）")
        finally:
            for f in backups:
                f.write_text(originals[f], encoding="utf-8")

        # ================================================================
        print("\n--- 只删一侧：验证「一致性断言」确实抓得住---")
        # ================================================================
        src = originals[HEADERS_TS]
        patched = re.sub(r"(\n\s*['\"]x-dept-id['\"],)", "", src)
        if patched != src:
            HEADERS_TS.write_text(patched, encoding="utf-8")
            try:
                out = subprocess.run(
                    [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
                    capture_output=True, text=True, cwd=str(ROOT),
                )
                red_line = [
                    ln for ln in out.stdout.splitlines()
                    if "TS 侧清单 = Python 侧清单" in ln and "[FAIL]" in ln
                ]
                report("⚠️ 只删 TS 侧 → 「两侧一致」那条**转红**（跨端契约断言有效）",
                       bool(red_line), red_line[0].strip() if red_line else "（没红 —— 跨端断言无效）")
            finally:
                HEADERS_TS.write_text(src, encoding="utf-8")

        # ================================================================
        print("\n--- 还原自查 ---")
        # ================================================================
        all_restored = True
        for f in backups:
            same = f.read_text(encoding="utf-8") == originals[f]
            if not same:
                all_restored = False
                f.write_text(originals[f], encoding="utf-8")
                print(f"  !! {f.name} 未还原，已强制还原")
        report("三份源码都已还原", all_restored)

        out = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "test_module14_trust_boundary.py")],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        green = "[FAIL]" not in out.stdout
        tail = [ln for ln in out.stdout.splitlines() if "回归结果" in ln]
        report("还原后重跑主测试恢复全绿", green, tail[0].strip() if tail else "")

        # 编译不能坏（改了 TS 又还原，理论上没事，但还原失败过一次）
        tsc = subprocess.run(
            ["npx", "tsc", "--noEmit"], cwd=str(ROOT / "gateway"),
            capture_output=True, text=True,
        )
        report("还原后网关 tsc --noEmit 仍通过", tsc.returncode == 0, tsc.stderr[-200:])

    finally:
        for f, text in originals.items():
            f.write_text(text, encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  反向验证：{len(PASSES)} 项符合预期 / {len(FAILURES)} 项不符合")
    if FAILURES:
        for n in FAILURES:
            print(f"    ✗ {n}")
    print("  判据：每条 [RED] 表示「拆掉守卫后对应断言确实转红」——那才是合格。")
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())