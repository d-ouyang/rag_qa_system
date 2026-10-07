# pyright: basic
"""
P2-12c 反向验证 —— **真的把会话归属守卫拆掉，然后看断言转红**。

    .venv/bin/python tests/test_module15_session_isolation_reverse.py

--------------------------------------------------------------------------
为什么必须有这个文件
--------------------------------------------------------------------------
`tests/test_module15_session_isolation.py` 里有 20 多条「A 读不到 B」「A 改不了 B」
的断言。它们的可信度取决于一件事：**那些 403 是归属判据给的，还是别的东西给的**。

最危险的失败模式不是「守卫写了但写反了」（那种情况成对断言里的一半会红），
而是「守卫根本没被执行到，请求在更早的一步就失败了」——
比如接口层 404、比如鉴权 401、比如路由压根没挂。这些都会让
「A 读不到 B」变绿，而隔离**完全没生效**。

--------------------------------------------------------------------------
第一版的三处自身失败（都是「崩掉被当成红」）
--------------------------------------------------------------------------
    ① 拆读侧谓词时写了 `SESS.c.id == sa_lit(True)` —— SQLAlchemy 报
       ArgumentError，主测试崩在import，一条断言都没跑到，
       而反向验证只看到「输出里找不到那条断言」。**崩 ≠ 红**（第 65 条）。
    ② 拆 `save` 的越权抛错，主测试**全绿**。这不是「守卫没用」，
       而是接口层那道预检先挡下来了 —— `save` 是**第二道**防线。
       拆第二道而第一道还在，当然什么都不红。这条必须拆**两道**。
    ③ 主测试里的桩链写成「一被调用就抛 AssertionError」，
       于是预检被拆 → 请求进桩 → TestClient 把服务端异常重抛到调用方 →
       流式那条变成 `ExceptionGroup` → 整个测试文件崩掉。
       结果又变成「崩」而不是「红」。桩链**必须返回正常响应**。

所以下面每一条都是**先手工验证过红点**再写进来的 ——
反向验证自己红了就是在验证「断言到底能不能守住」，而不是在验证隔离。

--------------------------------------------------------------------------
拆哪几处，为什么是这几处
--------------------------------------------------------------------------
1. `_owner_match` 去掉归属条件       ← 读侧判据（历史 + 列表）  → 预期多条红
2. 接口层预检 + `save` 越权抛错一起拆 ← 写侧两道防线            → 预期 403 那几条红
3. `list_sessions` 传 owner_id=None   ← **接口层忘传归属**（最贴近真实事故的漏法）
4. `is_foreign_to` 恒返回 False      ← 403 与「不存在」的区分失效
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MYSQL_STORE = ROOT / "core" / "mysql_store.py"
MEMORY_MANAGER = ROOT / "core" / "memory_manager.py"
QA_ROUTES = ROOT / "api" / "routes" / "qa.py"
MAIN_TEST = ROOT / "tests" / "test_module15_session_isolation.py"

FAILURES: list[str] = []
PASSES: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    mark = "  [OK]  " if ok else "  [RED] "
    print(f"{mark}{name}" + (f" | {detail}" if detail else ""))


def run_main_test() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MAIN_TEST)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "EMBEDDING_BACKEND": "local", "RERANK_BACKEND": "local"},
    )


def red_lines(out: subprocess.CompletedProcess[str], keyword: str) -> list[str]:
    return [
        ln.strip() for ln in out.stdout.splitlines()
        if "[FAIL]" in ln and keyword in ln
    ]


def crash_reason(out: subprocess.CompletedProcess[str]) -> str:
    """返回「崩了的原因」；没崩返回空串。

    ⚠️ 必须单独判断「崩」与「红」（第 65 条）：崩掉只说明改法影响了主测试的运行，
    说不清是「守卫被拆掉了」还是「改法写错了」。这两者必须能分开，
    否则反向验证会「因为自己写错了而红」，而那份红没有任何信息量。
    """
    if "Traceback (most recent call last)" in out.stderr:
        return out.stderr.strip().splitlines()[-1][:160]
    if out.returncode not in (0, 1):
        return f"退出码 {out.returncode}"
    return ""


# --------------------------------------------------------------------------- #
# 四种拆法（全部经过手工验证，标注了预期红点）
# --------------------------------------------------------------------------- #
#: 拆法 1：读侧归属谓词失效 —— 只看 id，不看 user_id
BREAK_OWNER_PREDICATE = (
    "    if owner_id is None:\n"
    "        return SESS.c.user_id.is_(None)\n"
    "    return sa_or(SESS.c.user_id == owner_id, SESS.c.user_id.is_(None))",
    "    return SESS.c.id.is_not(None)",
)

#: 拆法 2a：接口层的越权预检失效（`ask` 里的那个 if）
BREAK_PRECHECK = (
    "    if get_memory_manager().is_foreign(session_id, owner_id=actor.id):\n"
    '        raise HTTPException(status_code=403, detail="无权访问该会话")\n'
    '    logger.info("收到问答请求',
    "    if False:\n"
    '        raise HTTPException(status_code=403, detail="无权访问该会话")\n'
    '    logger.info("收到问答请求',
)

#: 拆法 2b：存储层 save 的越权抛错失效
BREAK_SAVE_RAISE = (
    "            if current_owner is not None and current_owner != owner_id:",
    "            if False:",
)

#: 拆法 3：逻辑层 list_sessions 忘了把归属透下去（传成 None = 只认无主）
BREAK_LIST_PASSTHROUGH = (
    "        for session_id in self.store.list_ids(owner_id=owner_id):",
    "        for session_id in self.store.list_ids(owner_id=None):",
)

#: 拆法 4：is_foreign_to 恒返回 False（越权被当成「不存在」）
BREAK_IS_FOREIGN = (
    "        if actual is None:\n"
    "            # 「不存在」与「无主」在这里被压成同一个 None。\n"
    "            # 对这个方法而言**恰好是对的**：两者都不是 foreign ——\n"
    "            # 无主会话对所有人可认领，不该被判成「别人的」。\n"
    "            # 代价是「不存在」也要查一次，但调用方本来就已经查过了，\n"
    "            # 这条只是把「403 还是 200+空」的判断收敛到一个方法里。\n"
    "            return False\n"
    "        # owner_id=None 时任何已归属会话都不是自己的（它是别人的，只是我不知道是谁）\n"
    "        return True if owner_id is None else int(actual) != int(owner_id)",
    "        return False",
)


def apply(root_files: dict[Path, str], target: Path, pair: tuple[str, str]) -> bool:
    """按拆法改一个文件；返回「拆法是否命中」。"""
    src = root_files[target]
    old, new = pair
    if src.count(old) != 1:
        return False
    target.write_text(src.replace(old, new, 1), encoding="utf-8")
    return True


def main() -> int:
    print("=" * 70)
    print("  P2-12c 反向验证：拆掉归属守卫，断言必须转红")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="p2-12c-reverse-"))
    targets = (MYSQL_STORE, MEMORY_MANAGER, QA_ROUTES)
    originals = {f: f.read_text(encoding="utf-8") for f in targets}
    for f in targets:
        shutil.copy2(f, tmp / f.name)

    def restore() -> None:
        for f, text in originals.items():
            f.write_text(text, encoding="utf-8")

    try:
        # ================================================================
        print("\n--- 基线：不拆任何东西，主测试必须全绿 ---")
        # ================================================================
        out = run_main_test()
        base_crash = crash_reason(out)
        report("基线：主测试能正常跑完（没崩）", not base_crash, base_crash)
        report("基线：主测试全绿", "[FAIL]" not in out.stdout,
               f"{out.stdout.count('[FAIL]')} 条红")
        if base_crash or "[FAIL]" in out.stdout:
            print("  基线就不对，反向验证无从谈起（先修主测试）")
            return 1

        # ================================================================
        print("\n--- 反向验证 1：拆掉读侧归属谓词（历史 + 列表）---")
        # ================================================================
        report("反向验证 1 的拆法生效",
               apply(originals, MYSQL_STORE, BREAK_OWNER_PREDICATE))
        try:
            out = run_main_test()
            report("  └ 没有把主测试跑崩（崩了说明改法错，不是断言红）",
                   not crash_reason(out), crash_reason(out))
            hits = red_lines(out, "读 Bob 的会话历史")
            report("⚠️ 「Alice 读 Bob 的会话历史 → 403」**转红**",
                   bool(hits), hits[0] if hits else "（没红 —— 那条断言是别的东西在撑）")
            n = out.stdout.count("[FAIL]")
            report("⚠️ 转红的是**多条**（读侧是主防线，塌了要一片红）",
                   n >= 5, f"{n} 条转红")
            report("  └ 而且列表隔离也跟着塌（同一个谓词）",
                   bool(red_lines(out, "列表里没有 Bob 的会话")), "")
        finally:
            restore()

        # ================================================================
        print("\n--- 反向验证 2：拆掉写侧**两道**防线（接口预检 + save 抛错）---")
        # ================================================================
        # ⚠️ 这里必须两道一起拆。第一版只拆 save → 主测试**全绿**，
        # 因为接口层的预检先挡住了。单独拆第二道防线什么都测不到 ——
        # 这不是「save 的守卫没用」，而是「**它本来就是第二道**」。
        # 写进注释是因为下一次有人做反向验证时，大概率也会只拆一道。
        ok2a = apply(originals, QA_ROUTES, BREAK_PRECHECK)
        ok2b = apply(originals, MYSQL_STORE, BREAK_SAVE_RAISE)
        report("反向验证 2 的拆法生效（两道都命中）", ok2a and ok2b,
               f"接口预检={ok2a} save抛错={ok2b}")
        try:
            out = run_main_test()
            report("  └ 没有把主测试跑崩", not crash_reason(out), crash_reason(out))
            hits = red_lines(out, "追加提问")
            report("⚠️ 「Alice 往 Bob 会话追加提问 → 403」**转红**",
                   bool(hits), hits[0] if hits else "（没红 —— 写侧没在守）")
            # ⚠️ truncate / PATCH / DELETE / 流式这四条**不会**跟着红，
            # 因为它们各自还有**另一条**独立防线：
            #   · truncate / PATCH → `_session_missing_or_forbidden()` 里再问一次 is_foreign
            #   · DELETE         → `delete_session()` 里 clear 失败后再问一次 is_foreign
            #   · 流式           → `ask_stream` 自己的预检（拆法 2a 只动了 ask）
            # 第一版把它们的「转红」也列进判据，于是 3 条 [RED] —— 那不是隔离有多层，
            # 而是反向验证拆得**不完整**。这里反过来断言它们**仍然绿**：
            # 这才是「每条路由各自有守卫」的证据。
            still_green = [kw for kw in ("截断 Bob 的会话", "改名 Bob 的会话",
                                         "删 Bob 的会话", "流式接口越权")
                           if red_lines(out, kw)]
            report("⚠️ 且 truncate/PATCH/DELETE/流式 仍然绿（各有独立防线）",
                   not still_green, f"这些反而红了：{still_green}")
        finally:
            restore()

        # ================================================================
        print("\n--- 反向验证 3：逻辑层忘传归属（最贴近真实事故的漏法）---")
        # ================================================================
        # 存储层判据**完全正确**，只是 `list_sessions` 把 owner_id 传成了 None。
        # 这是 12c 实施时最容易犯的错，也是最难靠「跑一遍存储契约」发现的错。
        report("反向验证 3 的拆法生效",
               apply(originals, MEMORY_MANAGER, BREAK_LIST_PASSTHROUGH))
        try:
            out = run_main_test()
            report("  └ 没有把主测试跑崩", not crash_reason(out), crash_reason(out))
            hits = red_lines(out, "列表里有自己的会话")
            report("⚠️ 列表隔离转红（传 None = 只认无主，于是谁也看不到自己的）",
                   bool(hits), hits[0] if hits else "")
            hits2 = red_lines(out, "Bob 的列表里有自己的会话")
            report("⚠️ Bob 侧同样转红",
                   bool(hits2), hits2[0] if hits2 else "")
        finally:
            restore()

        # ================================================================
        print("\n--- 反向验证 4：is_foreign_to 恒返回 False（403/404 区分失效）---")
        # ================================================================
        report("反向验证 4 的拆法生效",
               apply(originals, MYSQL_STORE, BREAK_IS_FOREIGN))
        try:
            out = run_main_test()
            report("  └ 没有把主测试跑崩", not crash_reason(out), crash_reason(out))
            hits = red_lines(out, "认领后 Bob 读它")
            report("⚠️ 「认领后 Bob 读它 → 403」**转红**",
                   bool(hits), hits[0] if hits else "")
            report("⚠️ 「Alice 往 Bob 会话追加提问 → 403」**也转红**",
                   bool(red_lines(out, "追加提问")), "")
            # 关键的一条：读侧判据还在，所以**内容一条都没漏** ——
            # 变红的只是错误码（403 → 200 + 空列表）。
            # 这恰好证明 403 与「读不到内容」是**两件独立的事**：
            # 一个坏了另一个照常工作，所以光断言「读不到内容」是不够的。
            #
            # ⚠️ 这里**不能**用「越权响应体里没有任何消息内容」那条当证据：
            # 它断的是 `"messages" not in json`，而 200 的响应里那个键是存在的
            # （值为[]），于是它照样红 —— 红了不代表泄漏，
            # 只代表「响应形态从 403 变成了 200」。第一版就是踩了这个：
            # 把「机制不同」误读成「防线也塌了」。
            # 真正证明独立的是「403 的文案不泄漏内容」——它断的是**内容**，
            # 而 200+空列表同样不含任何一条消息，所以它保持绿。
            report("⚠️ 但「403 的文案不泄漏内容」仍然绿（证明内容一条都没漏）",
                   not red_lines(out, "403 的文案不泄漏内容"),
                   "（它也红了 = 内容真的漏出去了，那说明 4 号拆法拆错了地方）")
            report("  └ 而「越权响应体里没有任何消息内容」会红（响应形态变了：403 → 200+空）",
                   bool(red_lines(out, "越权响应体里没有任何消息内容")),
                   "—— 这是预期的：它断的是键的有无，不是内容")
        finally:
            restore()

        # ================================================================
        print("\n--- 还原自查 ---")
        # ================================================================
        all_restored = True
        for f, text in originals.items():
            if f.read_text(encoding="utf-8") != text:
                all_restored = False
                f.write_text(text, encoding="utf-8")
                print(f"  !! {f.name} 未还原，已强制还原")
        report("三份源码都已还原", all_restored)

        out = run_main_test()
        report("还原后重跑主测试恢复全绿", "[FAIL]" not in out.stdout,
               f"{out.stdout.count('[FAIL]')} 条红")
        tail = [ln for ln in out.stdout.splitlines() if "结果" in ln]
        if tail:
            print(f"    {tail[-1].strip()}")

    finally:
        restore()
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