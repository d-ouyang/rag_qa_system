#!/usr/bin/env python
"""
知识库护栏 —— **跑回归之前先确认「本机有真知识库」，有就拒绝跑**。

--------------------------------------------------------------------------
为什么需要这个（2026-10-07 真实事故）
--------------------------------------------------------------------------
`tests/test_module9_async_pipeline.py` 的 `_cleanup_documents()` 曾经写的是
`sa_delete(document_table)` —— **全表清空，没有 where**。

跑了一轮 `make test` 之后：
  · MySQL `document` 表 **35 篇真实知识库文档的登记行全部消失**；
  · 而它们的 **90 个切片还留在 Chroma 里**（Chroma 不在那条DELETE 的射程内）。

于是前端「知识库管理」那一页出现了一个看起来像 bug 的现象：
    文档数 0     ← 读的是 `document` 表（空了）
    片段总数 90  ← 读的是 Chroma（还在）
**两个数字来自两个数据源**，于是没人看得出这是数据丢了，只觉得「界面算错了」，
而问答则一律回「根据现有资料无法回答」。

真正的损失只有登记行——原文件在 `upload/` 里一个没丢，
`scripts/reindex.py` 一次就补回来了（实测 35/35 全成功）。
但**不该由一次 `make test` 触发**，所以修成两道：

  ① **静态检查**（本脚本）：全仓扫「无 where 的 `sa_delete`」与
     「无 where 的 `DELETE FROM` / `TRUNCATE`」。全表删除在测试里几乎总是错的
     —— 测试要的是「清掉自己造的那几行」，不是「把表擦干净」。
     `scripts/reset_kb.py` 是**合法的**全清工具（默认干跑、必须显式 `--apply`），
     所以显式豁免它。
  ② **运行门禁**（本脚本）：`document` 表非空 → **拒绝跑 make test**。
     真要用测试，请把测试指向独立的库（见下面 `KB_GUARD_ALLOW`）。

--------------------------------------------------------------------------
为什么门禁是「拒绝」而不是「打个招呼」
--------------------------------------------------------------------------
  · 测试脚本有十几个，逐一加保护 = 十几个地方会漏；
  · 静态检查能挡住「已��存在的写法」，挡不住「下一个新写的」；
  · 真正难防的是**手滑**（写测试时顺手 `delete(table)`，事后忘了）。
    门禁把「手滑」的成本从「数据没了+ 一小时排查」降到「命令直接拒绝执行」。
所以是**硬拒**。要绕过必须显式声明理由，那一下就是有意识的决定。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    make test                    # 带门禁（本机有真知识库时会被拒）
    make test-sandbox            # 在一次性库 + 一次性 Chroma 上跑，不碰真数据
    KB_GUARD_ALLOW=1 make test    # 明知风险、强行在真库上跑（会打印警告与后果）

--------------------------------------------------------------------------
为什么不给「自动备份 + 自动恢复」
--------------------------------------------------------------------------
听起来更友好，实际是错的：
  · 自动备份会把「已经被清空」这件事**变成正常流程**，于是没人再关心它；
  · 恢复有窗口期，那段时间里知识库是残缺的，别人正好在这个窗口提问，
    得到的是残缺答案 —— 而这种错误比「直接报错」危险得多；
  · 多一份备份就多一份「备份本身是不是最新的」需要人维护的东西。
更好的做法是**让它压根跑不起来**，恢复只作为事故后的补救手段而非常规流程。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 扫描范围：测试与scripts。core/api 是生产代码，它们当然要能删数据
#（删自己的文档是业务功能），所以不在扫描范围内。
SCAN_DIRS = ("tests", "scripts")

# 合法的全清工具：默认干跑、必须显式 --apply、且有完整边界说明。
EXEMPT_FILES = {"scripts/reset_kb.py"}

# ⚠️ **本文件必须自我豁免** —— 它的 docstring 里就写着
# `c.execute("DELETE FROM document")` 这样的**反例示范**（要说明「为什么错」），
# 而原生 SQL 检查扫的就是字符串。所以不豁免的话，检查会把自己的文档判成违规，
# 然后所有人都会学会忽略它的输出 —— **一个永远红的检查等于没有检查**。
# 同理，下面 `_RAW_DELETE` 那条正则的字符串也是例子，不是违规代码。
EXEMPT_FILES.add("scripts/kb_guard.py")

# 匹配「无 where 的删除」。分成两类：
#   1) sa_delete(某表)      —— SQLAlchemy 形式
#   2) DELETE FROM x / TRUNCATE —— 原生 SQL 形式
# 判定「有没有 where」要跨行看，所以先把整个文件读进来再扫。
_SA_DELETE = re.compile(r"sa_delete\(\s*[A-Za-z_][A-Za-z0-9_.]*\s*\)")
_RAW_DELETE = re.compile(
    r"(?:DELETE\s+FROM|TRUNCATE\s+(?:TABLE\s+)?)\s*[`\"]?([A-Za-z_][A-Za-z0-9_]*)[`\"]?",
    re.IGNORECASE,
)


def _strip_comments_and_strings(src: str) -> str:
    """
    去掉注释、docstring 与字符串字面量，只留代码。

    ⚠️ 为什么必须去，而且必须处理 **docstring**：
    模块开头的说明文字里就会写「这里曾经写的是 `sa_delete(document_table)`」，
    不剔除的话静态检查会**把自己写的文档判成违规** ——
    然后所有人都会学会忽略它的输出，而一个永远红的检查等于没有检查。
    只处理 `#` / `/* */`是不够的：**docstring 是三引号字符串**，
    它在语法上就是字符串，必须按字符串处理（否则第一版就误判了两次）。

    实现方式：按字符扫，把注释/字符串**替换成等量空格 + 换行**，
    而不是删除 —— 删掉会让后续行号全部前移，报出来的行号对不上原文，
    而「行号对不上」会让排查的人怀疑检查本身有问题。
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        # ---- 注释 ----
        if ch == "#":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        # ---- 三引号（docstring 与多行字符串）----
        elif ch in ("'", '"') and src[i : i + 3] in ("'''", '"""'):
            q = src[i : i + 3]
            out.append("   ")
            i += 3
            while i < n and src[i : i + 3] != q:
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("   ")
            i += 3
            continue
        # ---- 前缀型字符串（r"..." / f"..." / b"..."）：跳过前缀字母 ----
        elif ch in ("r", "R", "b", "B", "f", "F", "u", "U") and i + 1 < n and src[i + 1] in ("'", '"'):
            i += 1
            out.append(" ")
            q = src[i]
            out.append(" ")
            i += 1
            while i < n and src[i] != q:
                if src[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
        # ---- 普通单行字符串 ----
        elif ch in ("'", '"'):
            q = ch
            out.append(" ")
            i += 1
            while i < n and src[i] != q:
                if src[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.suffix != ".py" or "__pycache__" in p.parts:
                continue
            if p.relative_to(ROOT).as_posix() in EXEMPT_FILES:
                continue
            out.append(p)
    return out


def _check_raw_sql(src: str, rel: str) -> list[str]:
    """
    单独扫**原生SQL 的全表删除** —— 必须在**未剥离字符串**的源码上做。

    ⚠️⚠️ 这是第一版的真实缺陷，反向验证才暴露出来：
    静态检查原本跑在「剥离了字符串」的代码上，于是
    `c.execute("DELETE FROM document")` 里的 SQL **正好在字符串里**，
    被自己剥掉了 —— 于是这个检查对**原生 SQL 完全无效**，
    而它显示为「✓ 通过」。一个永远绿的检查比没有检查更坏。

    为什么必须扫字符串：
    · SQLAlchemy 形式（`sa_delete(table)`）是**代码**，剥离后还在 → 能抓；
    · 原生 SQL（`execute("DELETE FROM x")`、`text("DELETE FROM x")`）
      **必然在字符串里** → 只有扫字符串才抓得到。
    而原生 SQL 恰恰是最容易漏掉 `WHERE` 的写法。

    判定规则：只看**字符串字面量内部**是否含 `DELETE FROM` / `TRUNCATE`
    且同一语句内没有 `WHERE`。这里不做注释剥离（注释里的示例不算违规），
    所以改用「字符串字面量提取」—— 逐个取出所有字符串字面量再判。
    """
    bad: list[str] = []
    lines = src.splitlines()
    for lineno, line in enumerate(lines, 1):
        for raw in _iter_string_literals(line):
            up = raw.upper()
            if "DELETE FROM" not in up and "TRUNCATE" not in up:
                continue
            # 归一化空白后逐句判断：分号分隔的语句里，只要**任何一句**是
            # 无 WHERE 的全表删除，就算违规（多语句里混一个就够危险）。
            for stmt in re.split(r";", raw):
                s = stmt.strip()
                if not s:
                    continue
                up2 = s.upper()
                m = _RAW_DELETE.search(s)
                if not m:
                    continue
                if not re.search(r"\bWHERE\b", up2):
                    bad.append(f"{rel}:{lineno}  字符串里的 {s.strip()[:60]!r} 没有 WHERE → 全表清空")
    return bad


def _iter_string_literals(line: str):
    """
    取出**一行内**所有普通字符串字面量的内容（不含引号）。

    只处理 `'...'` / `"..."` 与 `f"..."` 这类前缀形式；
    不跨行（多行三引号 SQL 很罕见，而跨行会让行号失去意义）。
    刻意**不**去处理 `#` 注释里的内容 —— 注释里的示例不是违规。
    但要注意 `c.execute("...")  # DELETE FROM x` 这种：注释在引号外，
    按 `_iter_string_literals` 的扫描顺序，注释部分的引号不会被当成字符串起点，
    所以天然不会误判。
    """
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        # 跳过注释
        if ch == "#":
            return
        # 前缀字母（r/f/b/u 及其组合，如 rf""）
        if ch.isalpha() and ch in "rRbBuUfF":
            j = i
            while j < n and line[j].isalpha() and line[j] in "rRbBuUfF":
                j += 1
            if j < n and line[j] in ("'", '"'):
                i = j
                ch = line[i]
            else:
                i = j
                continue
        if ch in ("'", '"'):
            q = ch
            # 检查是否是三引号（多行）—— 三引号内容不在本行内完整，跳过
            if line[i : i + 3] in ("'''", '"""'):
                return
            i += 1
            buf = []
            while i < n and line[i] != q:
                if line[i] == "\\" and i + 1 < n:
                    buf.append(line[i + 1])
                    i += 2
                    continue
                buf.append(line[i])
                i += 1
            yield "".join(buf)
            i += 1
            continue
        i += 1


def count_upload_files() -> int:
    """
    数 `upload/` 里的文件数（知识库**原文件**，Chroma 的 source 指向它们）。

    ⚠️ 为什么必须单独查它，而不能只看 `document` 表（2026-10-07 实测教训）：
    那一轮 `upload/` 里的 35 个原文件全没了，而 `document` 表的 35 条记录**一直好好的**
    —— 因为清文件的代码和清表的代码是两回事。
    于是只看表会给出「一切正常」的结论，而实际上：
      · 下载接口报「磁盘文件已丢失」
      · 重解析报「磁盘文件已丢失，无法重新解析」
      · 问答还能答（切片在 Chroma 里），**于是更晚才被发现**
    一行统计就能拦住的事，不该靠问答报错来发现。
    """
    d = ROOT / "upload"
    if not d.is_dir():
        return -1  # 目录都不在了，这本身就是最严重的一种
    return len([p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")])


def check_static() -> list[str]:
    """返回违规清单（空 = 通过）。"""
    bad: list[str] = []
    for p in _files():
        src = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(ROOT).as_posix()
        code_lines = _strip_comments_and_strings(src).splitlines()
        for i, line in enumerate(code_lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # 情况一：sa_delete(表) —— 它是**代码**，剥离字符串后仍然在。
            # 可能是跨行写法：sa_delete(\n  表\n).where(...)
            if _SA_DELETE.search(line) and ".where" not in line:
                seg = code_lines[i - 1 : i + 4]
                if not any(".where" in s for s in seg):
                    bad.append(f"{rel}:{i}  sa_delete(...) 没有 where →全表清空")
        # 情况二：原生 SQL —— 在**字符串里**，必须扫未剥离的源码。
        bad.extend(_check_raw_sql(src, rel))
    return bad


def check_upload_refs() -> list[str]:
    """
    核对 `document.storage_path` 指向的文件是否都在（反向检查）。

    这与 `count_upload_files` 是互补的两个方向：
      · 前者问「upload/ 里还剩几个」—— 能发现「被清空了」
      · 这个问「表里指的那些还在不在」—— 能发现「被删了/ 改名了」
    只查一个方向都会漏：文件被单独删掉时，前者的总数可能恰好还对。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from dotenv import load_dotenv

        load_dotenv()
        from sqlalchemy import text

        from core.db import get_engine

        with get_engine().connect() as c:
            rows = c.execute(text("SELECT file_name, storage_path FROM document")).fetchall()
    except Exception as e:  # noqa: BLE001
        return [f"（查不到 document 表，跳过这这项检查：{type(e).__name__}）"]
    missing = [
        (fn, sp) for fn, sp in rows if not (ROOT / sp).exists()
    ]
    if not missing:
        return []
    out = [f"document 有 {len(missing)} 条指向的文件不存在："]
    for fn, sp in missing[:5]:
        out.append(f"    {fn} → {sp}")
    if len(missing) > 5:
        out.append(f"    … 另有 {len(missing) - 5} 条")
    return out


def count_real_docs() -> int | None:
    """数 `document` 表里有多少行。返回 None = 查不到（不该发生，MySQL 没连上）。"""
    try:
        sys.path.insert(0, str(ROOT))
        from dotenv import load_dotenv

        load_dotenv()
        from sqlalchemy import text

        from core.db import get_engine

        with get_engine().connect() as c:
            return int(c.execute(text("SELECT COUNT(*) FROM document")).scalar() or 0)
    except Exception as e:  # noqa: BLE001 —— 查不到就说查不到，不要在这里崩
        print(f"  ⚠️ 连不上 MySQL：{type(e).__name__}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="知识库护栏：静态检查 + 有真知识库时拦下make test")
    ap.add_argument("--static-only", action="store_true", help="只跑静态检查，不查库")
    ap.add_argument("--count-only", action="store_true", help="只查库并报告，不做检查")
    args = ap.parse_args()

    if args.count_only:
        n = count_real_docs()
        f = count_upload_files()
        print(f"document 表行数：{'查不到' if n is None else n}")
        print(f"upload/ 原文件数：{'目录不存在⚠️' if f < 0 else f}")
        # 一次性给出「和上次比」的判断依据：两个数都记下来，
        # 下次跑完再跑一次这个命令，两个数字一比就知道有没有被清。
        if n is not None and f >= 0 and n != f:
            print()
            print(f"⚠️ 两个数不等（{n} vs {f}）—— 这**不一定**是问题：")
            print("   · 有文档正在解析/失败 → document 有行、文件已不在")
            print("   · 刚上传还没登记 → 文件多出来")
            print("   但值得看一眼：make kb-guard 里第③ 项会逐条核对 storage_path。")
        return 0

    print("=" * 68)
    print("  知识库护栏")
    print("=" * 68)

    # ---- ① 静态检查 ----
    print("\n[①] 静态检查：扫全仓「无 where 的删除」")
    bad = check_static()
    if bad:
        print(f"  ❌ 发现 {len(bad)} 处全表删除：")
        for b in bad:
            print(f"     {b}")
        print()
        print("     这些几乎一定是错的 —— 测试要的是「清掉自己造的那几行」。")
        print("     正确写法：sa_delete(表).where(表.c.file_name.like(f'前缀%'))")
        print("     若确实要全清，用 scripts/reset_kb.py（默认干跑 + 需显式 --apply）。")
        return 1
    print("  ✓ 没有无 where 的删除")

    if args.static_only:
        # 语义是「只跑这道检查」—— 那就该在这里返回。
        # 第一版忘了 return，于是它接着去查库并打印「拒绝执行 make test」，
        # 而那条信息在 `--static-only` 的语境下是误导（调用者只想知道检查结果）。
        print("\n（--static-only：跳过数据护栏）")
        return 0

    # ---- ② 有真知识库就拦下 ----
    print("\n[②] 数据护栏：检查本机是否有真实知识库")
    n = count_real_docs()
    files = count_upload_files()
    print(f"  document 表：{n if n is not None else '查不到'} 篇")
    print(f"  upload/ 原文件：{files if files >= 0 else '目录不存在⚠️'} 个")

    # ---- ③ 一致性反查：表里指的文件还在不在 ----
    print("\n[③] 一致性：document.storage_path 指向的文件是否都在")
    miss = check_upload_refs()
    if miss and miss[0].startswith("（"):
        print(f"  ⚠️ {miss[0][1:-1]}（不阻断）")
    elif not miss:
        print("  ✓ 全部在位")
    else:
        print(f"  ❌ {miss[0]}")
        for m in miss[1:]:
            print(f"  {m}")
        print()
        print("     这说明知识库的**原文件**丢了（切片还在 Chroma，所以问答还能答，")
        print("     但下载/重解析会报「磁盘文件已丢失」）。")
        print("     补救：文件能从 Chroma 切片重建（切片正文 + source 里记着原路径），")
        print("     重建后跑 make kb-restore 让 MySQL 与磁盘对齐。")
        return 1

    if n is None:
        print("\n  ⚠️ 查不到 document 表，跳过数据护栏（不阻断）")
        return 0
    if n == 0:
        print("\n  ✓ 本机没有真实知识库，允许跑测试")
        return 0

    if __import__("os").environ.get("KB_GUARD_ALLOW") == "1":
        print()
        print("  ⚠️⚠️ KB_GUARD_ALLOW=1 —— 你明确要求在真库上跑测试。")
        print("     后果：测试脚本会与真实数据同库；一旦有脚本清错范围，")
        print("     知识库就会残缺（表现为「文档数 0」或「文件已丢失」）。")
        print(f"     跑完请立刻核对：make kb-guard-count（应有 {n} 篇 / {files} 个文件）")
        return 0

    print()
    print("  ❌ 拒绝执行 make test —— 本机有真实知识库，测试脚本可能清错范围。")
    print()
    print("     历史教训（2026-10-07 真实发生两次）：")
    print("       ① 35 篇文档的登记行被 module9 的全表 DELETE 清掉，")
    print("          而 90 个切片留在 Chroma → 界面显示「文档数 0 / 片段总数 90」；")
    print("       ② 后来 upload/ 里的 35 个原文件也丢了一次，而 document 表一直完好——")
    print("          问答还能答，所以更晚才发现（下载/重解析报「磁盘文件已丢失」）。")
    print()
    print("     三个选项：")
    print("       1) make test-sandbox        ← 推荐。在一次性库上跑，真数据不受影响")
    print("       2) KB_GUARD_ALLOW=1 make test    明知风险，强行在真库上跑")
    print("       3) 把真知识库搬走再跑（不建议，搬运本身有风险）")
    print()
    print(f"     恢复：make kb-restore")
    print(f"     核对：make kb-guard-count   （现在应该是 {n} 篇 / {files} 个文件）")
    return 1


if __name__ == "__main__":
    sys.exit(main())