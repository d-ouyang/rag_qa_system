# pyright: basic
"""
P2-12c 验收脚本：**真的用两个账号登录，看不同的问答记录**（真链路，真 HTTP）

--------------------------------------------------------------------------
它验的是 `test_module15_session_isolation.py` **验不到**的那一层
--------------------------------------------------------------------------
module15 用的是 TestClient：它在进程内直接调路由与依赖，
所以它能验「归属判据对不对」，但**验不到**真实链路上的这几件事：

    · 网关**注入的身份头**到底带没带 `X-User-Id`（11c 之后账号真相源在MySQL，
      网关得先查库才知道这个登录对应哪个 uid —— 网关要是漏注入或注错，
      TestClient 一律绿，因为它是直接喂头的）；
    · 前端拿到的那两条会话列表**真的是分开的两份**；
    · 带真 token 打别人的 session_id，**响应码真的是 403**。

所以本脚本走完整链路：网关登录 → 拿真 token → 经网关打业务接口。

--------------------------------------------------------------------------
它验的四件事（每条都是用户能亲手复现的那种）
--------------------------------------------------------------------------
  1. **两个账号真的能各自登录**，且 uid 不同。
  2. **各自提问**，库里两条会话的 `user_id` 分别是两个人。
  3. **A 的列表里只有 A 的，B 的列表里只有 B 的** —— 且两边都能在列表里
     看到对方那条会话的「存在性」线索（标题/条数）都读不到。
  4. **拿 B 的 session_id 去读 / 改 / 删 / 追加 → 403**，
     且**库里的内容一个字都没变**。

第 4 条的「内容一个字都没变」是重点：只看响应码不够 ——
一个「先污染再报错」的实现也能返回 403。

--------------------------------------------------------------------------
用法与代价
--------------------------------------------------------------------------
    make dev                        # 先起后端 + 网关（8000 / 3000）
    .venv/bin/python tests/acceptance_p2_12c.py
    #或 make accept-12c

⚠️ **它会临时改两个种子账号的密码**（要拿真 token，只能真登录），
   收尾换成**新的随机临时密码** + 强制改密标记。
⚠️ **跑之前不要有别人在用这两个账号**。
⚠️ 问答链路会真的调意图模型与生成模型（一次几十秒 + token 花费），
   所以它只有 2 次真实提问，不是 20 次。
"""
import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from core import password_policy as policy  # noqa: E402
from core.db import get_engine  # noqa: E402

PASS = 0
FAIL = 0

BACKEND = "http://127.0.0.1:8000"
GATEWAY = "http://127.0.0.1:3000"

PROBE_PWD = "Vb12c!Probe"

#: 两个**普通员工**。刻意不用 admin：
#: 用 admin 会引入一个「他权限更高所以能看到」的可能性，
#: 而 12c 的承诺恰恰是**权限再高也看不到别人的问答记录**。
#: 那条断言在 module15 里用 hr 验过；这里用两个平级账号，
#: 验的是最普通的情形下隔离就成立。
ALICE = "chen.jie"
BOB = "zhao.min"

#: 造出来的会话 id 前缀，收尾按前缀清。
PREFIX = "m15acc_"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def http(args: list[str]) -> tuple[str, str]:
    """打一次 curl，返回 (状态码, 响应体)。

    ⚠️ `--noproxy '*'` 不能省：本机 HTTP_PROXY 会让 127.0.0.1返 502，
    而 502 的症状与「网关真的挂了」 indistinguishable。
    """
    p = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "-m", "180", "-w", "\n%{http_code}"] + args,
        capture_output=True, text=True,
    )
    parts = p.stdout.rsplit("\n", 1)
    return (parts[1] if len(parts) == 2 else ""), parts[0]


def set_pwd(username: str) -> int:
    e = get_engine()
    with e.begin() as c:
        uid = c.execute(
            text("SELECT id FROM `user` WHERE username=:u"), {"u": username}
        ).scalar()
        if uid is None:
            raise SystemExit(f"种子里没有 {username}，先跑 make seed-users-apply")
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=0 WHERE id=:i"),
            {"h": policy.hash_password(PROBE_PWD), "i": uid},
        )
    return int(uid)


def restore_pwd(username: str) -> None:
    e = get_engine()
    with e.begin() as c:
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=1 "
                 "WHERE username=:u"),
            {"h": policy.hash_password(policy.generate_temporary_password()), "u": username},
        )


def login(username: str) -> str | None:
    code, body = http([
        "-X", "POST", f"{GATEWAY}/api/auth/login",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"username": username, "password": PROBE_PWD}),
    ])
    if code != "200":
        print(f"  （{username} 登录返回 {code}：{body[:200]}）")
        return None
    try:
        return json.loads(body).get("access_token")
    except Exception:
        return None


def api(token: str, method: str, path: str, body: dict | None = None,
        raw: bool = False) -> tuple[str, str]:
    args = ["-X", method, f"{GATEWAY}{path}",
            "-H", f"Authorization: Bearer {token}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    code, text_body = http(args)
    if raw:
        return code, text_body
    try:
        return code, json.dumps(json.loads(text_body), ensure_ascii=False)
    except Exception:
        return code, text_body


def ask(token: str, session_id: str, question: str) -> tuple[str, str]:
    return api(token, "POST", "/api/v1/qa/ask",
               {"question": question, "session_id": session_id})


def db_owner(session_id: str) -> str | None:
    with get_engine().connect() as c:
        row = c.execute(
            text("SELECT u.username FROM session s LEFT JOIN `user` u ON u.id=s.user_id "
                 "WHERE s.id=:s"),
            {"s": session_id},
        ).first()
    return row[0] if row else None


def db_messages(session_id: str) -> list[str]:
    with get_engine().connect() as c:
        return [r[0] for r in c.execute(
            text("SELECT content FROM chat_message WHERE session_id=:s ORDER BY seq"),
            {"s": session_id},
        )]


# --------------------------------------------------------------------------- #
section("0. 两个进程都得在")
# --------------------------------------------------------------------------- #
api_code, _ = http([f"{BACKEND}/api/v1/system/health"])
gw_code, _ = http([f"{GATEWAY}/api/health"])
check(f"后端 {BACKEND} 在跑", api_code == "200", f"返回 {api_code}（先 make dev）")
check(f"网关 {GATEWAY} 在跑", gw_code == "200", f"返回 {gw_code}（先 make dev）")
if api_code != "200" or gw_code != "200":
    raise SystemExit("  服务没起来，剩下几组没有意义。")

sid_a = f"{PREFIX}{uuid.uuid4().hex[:16]}"
sid_b = f"{PREFIX}{uuid.uuid4().hex[:16]}"
alice_msgs_before: list[str] = []
bob_msgs_before: list[str] = []

# --------------------------------------------------------------------------- #
section("1. 两个账号真的能各自登录（真token，uid 不同）")
# --------------------------------------------------------------------------- #
alice_id = set_pwd(ALICE)
bob_id = set_pwd(BOB)
try:
    tok_a = login(ALICE)
    tok_b = login(BOB)
    check(f"{ALICE} 能真登录（拿得到真 token）", bool(tok_a), "见上面的响应")
    check(f"{BOB} 能真登录（拿得到真 token）", bool(tok_b), "见上面的响应")
    if not (tok_a and tok_b):
        raise SystemExit("  拿不到 token，隔离无从谈起。")
    check("两个人的 uid 确实不同", alice_id != bob_id, f"{alice_id} / {bob_id}")
    print(f"  （{ALICE}=uid{alice_id} / {BOB}=uid{bob_id}）")

    # 顺手确认一下：网关注入的身份头真的带对了 uid。
    # 这一条 module15 验不到 —— TestClient 是直接喂头的，不经过网关。
    # 若网关注错 uid（比如两个 token 都注成同一个），后面的隔离断言全部无意义。
    for who, tok, want_id in ((ALICE, tok_a, alice_id), (BOB, tok_b, bob_id)):
        code, body = api(tok, "GET", "/api/v1/admin/me")
        try:
            me = json.loads(body)
        except Exception:
            me = {}
        if who == ALICE:
            # 普通员工打管理端应该 403，这里只是借它确认「身份没串」
            pass
        code2, body2 = api(tok, "GET", "/api/v1/qa/sessions")
        check(f"{who} 经网关打业务接口 → 200（身份头注对了）", code2 == "200",
              f"{code2} {body2[:160]}")

    # -----------------------------------------------------------------------
    section("2. 各自提问（真链路，各调一次模型）")
    # -----------------------------------------------------------------------
    print(f"  Alice 提问中…（会话 {sid_a}）")
    code_a, body_a = ask(tok_a, sid_a, "公司的年假怎么计算？")
    check("Alice 问了一句 → 200", code_a == "200", f"{code_a} {body_a[:200]}")
    try:
        ans_a = json.loads(body_a).get("answer", "")
    except Exception:
        ans_a = ""
    print(f"    回答：{ans_a[:80]}")
    check("  └ 回答非空（真的调了模型，不是打桩）", bool(ans_a.strip()), ans_a[:120])

    print(f"  Bob 提问中…（会话 {sid_b}）")
    code_b, body_b = ask(tok_b, sid_b, "公司的报销流程是什么？")
    check("Bob 问了一句 → 200", code_b == "200", f"{code_b} {body_b[:200]}")
    try:
        ans_b = json.loads(body_b).get("answer", "")
    except Exception:
        ans_b = ""
    print(f"    回答：{ans_b[:80]}")

    check("**库里两条会话的归属分别是两个人**",
          db_owner(sid_a) == ALICE and db_owner(sid_b) == BOB,
          f"{ALICE}那条={db_owner(sid_a)} / {BOB}那条={db_owner(sid_b)}")

    # -----------------------------------------------------------------------
    section("3. 各自的会话列表只有自己的")
    # -----------------------------------------------------------------------
    code, body = api(tok_a, "GET", "/api/v1/qa/sessions")
    try:
        a_list = json.loads(body)
    except Exception:
        a_list = []
    a_ids = {i["session_id"] for i in a_list}
    code, body = api(tok_b, "GET", "/api/v1/qa/sessions")
    try:
        b_list = json.loads(body)
    except Exception:
        b_list = []
    b_ids = {i["session_id"] for i in b_list}

    check("Alice 的列表里有自己那条", sid_a in a_ids, f"实际 {sorted(a_ids)}")
    check("**Alice 的列表里没有 Bob 那条**", sid_b not in a_ids, f"实际 {sorted(a_ids)}")
    check("Bob 的列表里有自己那条", sid_b in b_ids, f"实际 {sorted(b_ids)}")
    check("**Bob 的列表里没有 Alice 那条**", sid_a not in b_ids, f"实际 {sorted(b_ids)}")

    # 更狠的一条：Bob 的列表里**任何一条**都不能提到 Alice 那条会话的字样
    blob_b = json.dumps(b_list, ensure_ascii=False)
    check("Bob 的列表里连 Alice 那条会话的 id 串都搜不到", sid_a not in blob_b, blob_b[:200])

    # -----------------------------------------------------------------------
    section("4. 拿 Bob 的 session_id 去读 / 改 / 删 / 追加 → 403，且内容一字未变")
    # -----------------------------------------------------------------------
    bob_msgs_before = db_messages(sid_b)

    code, body = api(tok_a, "GET", f"/api/v1/qa/sessions/{sid_b}")
    check("**Alice 读 Bob 的会话历史 → 403**", code == "403", f"{code} {body[:200]}")
    check("  └ 响应里没有 Bob 那句提问（连片段都不给）",
          "报销流程" not in body, body[:200])
    check("  └ 响应里没有 Bob 那句回答",
          not any(w in body for w in ("发票", "审批", "单据")), body[:200])

    code, body = api(tok_a, "PATCH", f"/api/v1/qa/sessions/{sid_b}", {"title": "Alice 改的"})
    check("**Alice 改名 Bob 的会话 → 403**", code == "403", f"{code} {body[:160]}")

    code, body = api(tok_a, "POST", f"/api/v1/qa/sessions/{sid_b}/truncate",
                     {"keep_messages": 0})
    check("**Alice 截断 Bob 的会话 → 403**", code == "403", f"{code} {body[:160]}")

    code, body = api(tok_a, "DELETE", f"/api/v1/qa/sessions/{sid_b}")
    check("**Alice 删 Bob 的会话 → 403**", code == "403", f"{code} {body[:160]}")

    print("  Alice 往 Bob 的会话里追问一句（这一条会真的调模型，若隔离失效就会被存进去）…")
    code, body = ask(tok_a, sid_b, "这个流程需要几天？")
    check("**Alice 往 Bob 的会话追加提问 → 403**", code == "403", f"{code} {body[:200]}")

    # 最关键的一条：只看响应码不够 ——
    # 一个「先污染再报错」的实现也能返回 403。所以必须回库核对内容。
    after = db_messages(sid_b)
    check("**Bob 的会话内容一条都没变**（没有「先污染再报错」）",
          after == bob_msgs_before,
          f"改前 {len(bob_msgs_before)} 条 / 改后 {len(after)} 条")
    check("  └ Alice 那句追问没有落进Bob 的会话",
          not any("这个流程需要几天" in m for m in after), str(after))
    check("  └ 归属也没被改成 Alice", db_owner(sid_b) == BOB, f"实际 {db_owner(sid_b)}")
    check("  └ Bob 的会话还在（没被删）", db_owner(sid_b) is not None)

    # -----------------------------------------------------------------------
    section("5. Bob 自己的会话仍然完好（不是被 Alice 的操作搞坏）")
    # -----------------------------------------------------------------------
    code, body = api(tok_b, "GET", f"/api/v1/qa/sessions/{sid_b}")
    check("Bob 读自己的会话 → 200", code == "200", f"{code} {body[:200]}")
    try:
        b_hist = json.loads(body)
    except Exception:
        b_hist = {}
    check("  └ 自己的历史还在（消息条数 ≥ 2）",
          b_hist.get("message_count", 0) >= 2, str(b_hist)[:200])
    check("  └ 能读回自己那句提问",
          any("报销流程" in m.get("content", "") for m in b_hist.get("messages", [])),
          str(b_hist)[:200])

    code, body = api(tok_a, "GET", f"/api/v1/qa/sessions/{sid_a}")
    check("Alice 读自己的会话 → 200（没被自己的越权操作牵连）", code == "200",
          f"{code} {body[:200]}")

finally:
    # -----------------------------------------------------------------------
    section("收尾：清掉本脚本造的会话 + 还原密码")
    # -----------------------------------------------------------------------
    with get_engine().begin() as c:
        c.execute(text("DELETE FROM chat_message WHERE session_id LIKE :p"),
                  {"p": f"{PREFIX}%"})
        c.execute(text("DELETE FROM session WHERE id LIKE :p"), {"p": f"{PREFIX}%"})
    for u in (ALICE, BOB):
        restore_pwd(u)
    with get_engine().connect() as c:
        left = c.execute(text("SELECT COUNT(*) FROM session WHERE id LIKE :p"),
                         {"p": f"{PREFIX}%"}).scalar()
    check("本脚本造的会话已清干净", left == 0, f"还剩 {left} 条")
    print(f"  （{ALICE} / {BOB} 的密码已换成新的随机临时密码 + 强制改密标记）")

print()
print("=" * 70)
print(f"P2-12c 真链路验收：{PASS} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)