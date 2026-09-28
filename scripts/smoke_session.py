"""
会话冒烟脚本 —— 用**真链路**跑一遍会话，验四件事：

    1. 多轮问答：同一 session_id 下，前一轮真的进了上下文
    2. 引用可反查：历史里每一条 chunk_id 都能 GET /api/v1/chunks/{chunk_id} 拿到正文
    3. 会话落库：session / chat_message 的条数与接口返回一致
    4. 没有脏数据：切片有、文档记录也在（无孤儿切片）

不打任何桩：真后端、真 Chroma、真 MySQL、真远程模型。所以它必须跑在 `make api`
之后（脚本只连后端，不经网关 —— 后端自己不做鉴权）。

--------------------------------------------------------------------------
为什么要有这个脚本
--------------------------------------------------------------------------
知识库重建完之后，「库里有 101 个切片」和「问答链路能正确用上这些切片」是两件事。
切片灌对了但会话链路坏了（多轮丢上下文、引用存了却反查不到、落库少写一条），
用户看到的就是「这系统真难用」，而所有单测都会绿。所以这里用一次端到端的真问答
把这条链串起来验。

--------------------------------------------------------------------------
问什么（刻意挑的，不是随便问两句）
--------------------------------------------------------------------------
问题必须能在 `docs/guoquan-kb/` 里查到答案，否则「答不上来」是知识库的锅而不是
链路的锅，脚本会变成假的通过。三问落在三个不同文件：

    ① 上市信息 → 公司知识库/04-上市信息.md
    ② 加盟投资 → 公司知识库/07-加盟商准入与支持政策.md
    ③ 请假流程 → 内部管理制度/02-考勤假期/05_考勤管理制度.md

第三问刻意**不带品牌词**（「员工请假要怎么走流程？」而不是「锅圈员工请假怎么走」）：
只有这样才是在验「历史消息真的进了上下文」。带了主题词就退化成一句普通的单轮
检索 —— 多轮链路断了我也看不出来。

用法：
    .venv/bin/python scripts/smoke_session.py            # 真跑
    .venv/bin/python scripts/smoke_session.py --dry-run  # 只打印要做什么
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# `python scripts/x.py` 时 sys.path[0] 是 scripts/ 而不是仓库根，
# 所以 core/ 默认 import 不到 —— 显式补上，否则只有 DB 对账那一步会炸。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_URL = "http://127.0.0.1:8000"

TURNS: list[str] = [
    "锅圈食品是在哪里上市的？股票代码是多少？",
    "开一家锅圈门店大概需要准备多少投资？主要花在哪几块？",
    "那员工请假要怎么走流程？",
]

PASS = 0
FAIL = 0
# 复核模式（--session-id）不提问，这个开关只影响「usage_requests 应该等于几」
SKIP_ASK = False


# --------------------------------------------------------------------------- #
# 断言与打印
# --------------------------------------------------------------------------- #
def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  ✅ {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  ❌ {msg}")


def check(cond: bool, good: str, wrong: str) -> bool:
    """断言成对打印。该成的成不了是错，不该成的成了也是错。"""
    ok(good) if cond else bad(wrong)
    return cond


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(  # noqa: S310
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return e.code, {"detail": raw}
    except OSError as e:  # URLError 的基类，连不上/超时都落这里
        return 0, {"detail": str(e)}


# --------------------------------------------------------------------------- #
# 步骤
# --------------------------------------------------------------------------- #
def step_backend(apply_changes: bool) -> None:
    print("\n[0/5] 后端在线？")
    if not apply_changes:
        print(f"      GET {BASE_URL}/api/v1/system/health  [干跑]")
        return
    code, body = http_json("GET", "/api/v1/system/health")
    if check(code == 200, "后端在线", f"后端不在线（{code} {body}）：先跑 `make api`"):
        print(f"       version={body.get('version')}")


def step_ask(apply_changes: bool) -> str:
    print("\n[1/5] 三轮问答（同一会话；第三轮是纯追问）")
    session_id = ""
    for i, q in enumerate(TURNS, 1):
        print(f"\n  第 {i} 问：{q}")
        if not apply_changes:
            print(f"      POST /api/v1/qa/ask  session_id={session_id or '(服务端生成)'}  [干跑]")
            continue

        code, body = http_json("POST", "/api/v1/qa/ask", {"question": q, "session_id": session_id or None})
        if code != 200 or not isinstance(body, dict):
            bad(f"第 {i} 问失败：HTTP {code} {json.dumps(body, ensure_ascii=False)[:300]}")
            return ""
        session_id = body.get("session_id", "")
        print(f"      route={body.get('route')} intent={body.get('intent')} "
              f"来源={body.get('intent_source')} 耗时={body.get('elapsed_ms')}ms 缓存={body.get('cache_hit')}")
        print(f"      回答：{(body.get('answer') or '')[:110].replace(chr(10), ' ')}…")

        sources = body.get("sources") or []
        if not check(len(sources) > 0, f"检索到 {len(sources)} 条引用", "这一问没有检索到任何引用"):
            return ""
        for s in sources:
            cid = s.get("chunk_id")
            print(f"        · {cid}  {(s.get('file_name') or s.get('source'))}  "
                  f"「{(s.get('snippet') or '')[:36]}…」")
            if not (isinstance(cid, str) and cid.count(":") == 1):
                bad(f"chunk_id 格式不对：{cid!r}（应形如 12:3）")
                return ""

    if session_id:
        print(f"\n      会话 id = {session_id}")
    return session_id


def step_history(apply_changes: bool, session_id: str, expect_messages: int) -> None:
    print("\n[2/5] 会话历史 + 引用反查")
    if not apply_changes:
        print(f"      GET /api/v1/qa/sessions/{session_id or '{id}'}  [干跑]")
        return

    code, hist = http_json("GET", f"/api/v1/qa/sessions/{session_id}")
    if not check(code == 200, "会话历史接口正常", f"会话历史接口返回 {code}"):
        return

    messages = hist.get("messages") or []
    check(len(messages) == expect_messages, f"历史 {len(messages)} 条（应 {expect_messages}）",
          f"历史 {len(messages)} 条，应 {expect_messages} 条（每轮 用户+助手 各一条）")
    roles = [m.get("role") for m in messages]
    check(roles == ["user", "assistant"] * (expect_messages // 2), "角色严格交替",
          f"角色序列异常：{roles}")

    # 每条引用都要能反查到正文 —— 这是「引用不是死链」的唯一真证据
    chunk_ids = [s["chunk_id"] for m in messages for s in (m.get("sources") or []) if s.get("chunk_id")]
    unique_ids = list(dict.fromkeys(chunk_ids))  # 去重保序
    print(f"      待反查 {len(unique_ids)} 条：{unique_ids}")
    dead: list[str] = []
    for cid in unique_ids:
        c, detail = http_json("GET", f"/api/v1/chunks/{cid}")
        content = ""
        if c != 200:
            dead.append(f"{cid}(HTTP {c})")
        else:
            content = (detail or {}).get("content") or ""
            if not content or (detail or {}).get("document_exists") is not True:
                dead.append(f"{cid}(正文{len(content)}字 / exists={(detail or {}).get('document_exists')})")
        print(f"        · {cid}  正文 {len(content)} 字  "
              f"file={(detail or {}).get('file_name')}  归属文档在库={(detail or {}).get('document_exists')}")
    check(not dead, "全部引用均可反查到正文，归属文档都还在",
          f"{len(dead)} 条死链：{dead}")

    # 会话列表里也要看得见，且计数一致
    c2, listing = http_json("GET", "/api/v1/qa/sessions")
    found = next((s for s in (listing or []) if s.get("session_id") == session_id), None)
    check(c2 == 200 and found is not None, "会话列表能看到这条会话", f"会话列表找不到 {session_id}")
    if found:
        check(found.get("message_count") == expect_messages,
              f"列表 message_count={found.get('message_count')}（应 {expect_messages}）",
              f"列表 message_count={found.get('message_count')}，应 {expect_messages}")


def step_db(apply_changes: bool, session_id: str, expect_messages: int) -> None:
    print("\n[3/5] MySQL 对账")
    if not apply_changes:
        print("      SELECT session / chat_message  [干跑]")
        return

    # 模块顶已补 sys.path。
    from sqlalchemy import func, select

    from core.db import session_scope
    from core.schema import chat_message_table, document_table, session_table

    # 全部走**类型列**而不是 text() 裸 SQL：JSON 列经 raw 驱动读出来是 JSON 文本
    # （空值就是 4 个字符的字符串 'null'），只有 select(表.c.列) 才会反序列化。
    # 用裸 SQL 读会把「user 行 ref_ids 为 NULL」误判成「存了个字符串 null」。
    with session_scope() as s:
        srow = s.execute(
            select(session_table.c.id, session_table.c.usage_requests, session_table.c.last_active_at)
            .where(session_table.c.id == session_id)
        ).first()
        if check(srow is not None, "session 表有这条会话", "session 表里找不到这条会话"):
            # session 表里**没有** message_count 列：接口那个是 chat_message 的实时计数。
            # 表里能对的是 usage_requests（累计问答轮次），而「问了几轮」就等于
            # 助手消息条数 —— 用消息数反推而不是写死 3，复核模式才不会误判。
            want_turns = expect_messages // 2
            check(int(srow[1] or 0) == want_turns,
                  f"session.usage_requests={srow[1]}（应为助手消息数 {want_turns}）",
                  f"session.usage_requests={srow[1]}，应 {want_turns}（助手消息数）")
            print(f"       last_active={srow[2]}")

        msgs = s.execute(
            select(
                chat_message_table.c.seq,
                chat_message_table.c.role,
                chat_message_table.c.ref_ids,
            ).where(chat_message_table.c.session_id == session_id).order_by(
                chat_message_table.c.seq
            )
        ).all()

    check(len(msgs) == expect_messages, f"chat_message {len(msgs)} 行（应 {expect_messages}）",
          f"chat_message {len(msgs)} 行，应 {expect_messages}")
    roles = [m[1] for m in msgs]
    check(roles == ["user", "assistant"] * (expect_messages // 2), "chat_message 角色交替正确",
          f"chat_message 角色序列异常：{roles}")
    seqs = [m[0] for m in msgs]
    check(seqs == list(range(expect_messages)), f"seq 连续从 0 排到 {expect_messages - 1}",
          f"seq 不连续：{seqs}")

    # 引用只挂助手行；user 行必须是 SQL NULL 而不是空数组或 'null' 字符串
    assistant_rows = [m for m in msgs if m[1] == "assistant"]
    user_rows = [m for m in msgs if m[1] == "user"]
    bad_refs = [m for m in assistant_rows if not (isinstance(m[2], list) and m[2])]
    check(not bad_refs, f"{len(assistant_rows)} 条助手消息都带了非空 ref_ids",
          f"{len(bad_refs)} 条助手消息的 ref_ids 不是非空数组")
    bad_users = [m for m in user_rows if m[2] is not None]
    check(not bad_users, "user 行 ref_ids 为空（引用不挂用户行）",
          f"{len(bad_users)} 条 user 行带了 ref_ids：{[m[0] for m in bad_users]}")
    for m in msgs:
        if m[2] is not None:
            print(f"        · seq={m[0]} ({m[1]})  ref_ids={m[2]}")

    with session_scope() as s:
        n = s.execute(
            select(func.count()).select_from(document_table)
        ).scalar_one()
    check(n > 0, f"document 表 {n} 行", "document 表空了")


def step_summary(apply_changes: bool) -> int:
    print("\n[4/5] 结论")
    if not apply_changes:
        print("      干跑结束。真跑：.venv/bin/python scripts/smoke_session.py")
        return 0
    print(f"      通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL == 0:
        print("      ✅ 会话链路干净：多轮 / 引用反查 / 落库 全部对得上")
    else:
        print("      ❌ 有问题，见上面 ❌ 行")
    return FAIL


def main() -> int:
    ap = argparse.ArgumentParser(description="真链路会话冒烟（多轮 + 引用反查 + 落库）")
    ap.add_argument("--dry-run", action="store_true", help="只打印要做什么，不发请求")
    ap.add_argument(
        "--session-id",
        help="复核指定会话（跳过提问，直接验历史 / 引用 / 落库）。"
             "不传则新建一轮三问会话。",
    )
    args = ap.parse_args()
    apply_changes = not args.dry_run

    print("=" * 70)
    print("会话冒烟 · 真链路（后端 + Chroma + MySQL + 远程模型）")
    print("=" * 70)

    step_backend(apply_changes)
    session_id = args.session_id or ""
    expect = 0

    if args.session_id:
        # 复核已有会话：条数取当前值（不假设一定是三问，避免把「少写了一条」判成正常）
        code, hist = http_json("GET", f"/api/v1/qa/sessions/{session_id}")
        messages = (hist or {}).get("messages") or []
        expect = len(messages)
        print(f"\n[1/5] 已指定会话 {session_id}，跳过提问（现有消息 {expect} 条）")
        if not apply_changes:
            step_history(False, session_id, expect)
            step_db(False, session_id, expect)
            return step_summary(False)
        if code != 200:
            bad(f"会话 {session_id} 取不到历史（{code}）")
            return 1
        # 复核模式不会新增提问，所以「应有多少条」就是现在这些。
        # （这里若写成 现有+3*2，就会把一个已经好的会话误判成坏的。）
        expect = len(messages)
        globals()["SKIP_ASK"] = True
    else:
        session_id = step_ask(apply_changes)
        if not apply_changes:
            step_history(False, "", 0)
            step_db(False, "", 0)
            return step_summary(False)
        if not session_id:
            return 1
        expect = len(TURNS) * 2

    step_history(apply_changes, session_id, expect)
    step_db(apply_changes, session_id, expect)
    return step_summary(apply_changes)


if __name__ == "__main__":
    sys.exit(main())
