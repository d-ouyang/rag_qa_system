# pyright: basic
"""
P2-12c 会话隔离回归测试。

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
「用户 A 能不能看到 / 读到 / 改到 / 删掉用户 B 的问答记录」。

12b 封的是**冒充**（拿自己的身份头去问别人的接口），
12c 封的是**看到别人的**—— 两者是不同的威胁：
12b 做完之后，A 完全可以合法地登录、合法地问问题，
只是如果他知道 B 的 session_id，就能读到 B 的全部历史。

--------------------------------------------------------------------------
为什么存储层之外还要在接口层再测一遍
--------------------------------------------------------------------------
存储契约（tests/store_contract.py）已经验证了三个 store 实现的归属语义，
但它验证的是「**判据**对不对」。这里验证的是另一件事：
**接口层有没有把身份接上去**。

这是两种完全不同的漏：
    · 存储判据写对了，但某个路由忘了挂 `Depends(current_actor)`
      → 该路由对所有人开放（`owner_id` 永远是那个默认值）
    · 存储判据写对了，但前端/网关没传身份头
      → 请求被401，看起来像「坏了」而不是「漏了」
这两种都不会让 store_contract 变红 —— 它只测 store，不知道路由的存在。

--------------------------------------------------------------------------
为什么必须「成对」断言（不能只测「A 读不到 B」）
--------------------------------------------------------------------------
「A 读不到 B」这条断言，在**隔离根本没实现**时也会绿——
只要接口层因为别的原因（比如鉴权挂了）一律返回 404/403。
所以每组都必须配一条「A 能读到自己」的断言：
隔离生效 ⟺ 读得到自己的 ∧读不到别人的。
只有前者是「功能正常」，只有后者是「功能生效」。

--------------------------------------------------------------------------
反向验证（tests/test_module15_session_isolation_reverse.py）
--------------------------------------------------------------------------
这里的每一条「该拒的拒了」都要有一条对应的反向验证：
把存储层的归属判据去掉，同一个断言必须转红。
否则守卫被写反，用例照样全绿。

运行：
    make infra
    .venv/bin/python tests/test_module15_session_isolation.py
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
#: 本模块造出来的会话 id 前缀。收尾按前缀清，不碰别人的行。
PREFIX = "m15iso_"

# 三个真实种子账号（与 module12 同一批人）。刻意用真实账号而不是临时造的：
# 隔离的判据是 user_id，用假 uid 测的是「数字相等」，不是「这个人的会话」。
ALICE = "chen.jie"      # 普通员工
BOB = "zhao.min"# 另一个普通员工
HR = "zhou.yan"         # hr：验证「管理端权限高 ≠ 能看别人的会话」


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module  # noqa: E402

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  SKIP：MySQL 不可达，P2-12c 未执行。")
    print("  本地起中间件：make infra")
    print("=" * 66)
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
print("  连通正常，开始执行\n")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from api.main import app  # noqa: E402
from config.settings import settings  # noqa: E402
from core import user_repo as repo  # noqa: E402
from core.identity import MODE_GATEWAY  # noqa: E402
from core.memory_manager import get_memory_manager  # noqa: E402

client = TestClient(app)

# 审计基线：12c 的动作会落审计（P2-13d），收尾要按 id 范围清掉，
# 不能按 target_label 前缀清 —— 审计行的标签会被改名带着变，按前缀会漏。
try:
    with db_module.get_engine().connect() as _c:
        AUDIT_BASELINE_MAX_ID = int(
            _c.execute(text("SELECT COALESCE(MAX(id), 0) FROM audit_log")).scalar() or 0
        )
except Exception:  # noqa: BLE001 - 表不存在属于「还没迁移」，不是错误
    AUDIT_BASELINE_MAX_ID = None

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 与 module12 的 `act_as` 同构，但这里**必须**发正确的网关证明 ——
#: 12b 之后 `IDENTITY_MODE=gateway` 下缺证明一律 401。
#: 若这里不发，所有断言都会因为「401」而红，而红的不是它们想验的东西；
#: 更坏的是有人会把 `s == 403` 的断言改成 `s == 401` 「修绿」，
#: 于是这份测试变成一条恒真的假绿（第 50 条）。
def headers_as(username: str) -> dict[str, str]:
    return {
        "X-User-Id": username,
        "X-Internal-Auth": settings.INTERNAL_SHARED_SECRET,
    }


def new_session_id() -> str:
    """前端同样是这么生成会话 id 的（crypto.randomUUID() 的形状）。"""
    return f"{PREFIX}{uuid.uuid4().hex[:16]}"


def write_as(username: str, session_id: str, question: str) -> None:
    """以某人的身份写一轮问答到存储层（不调 LLM）。

    ⚠️ **刻意不走POST /ask**：12c 要验的是「这段历史记在谁名下」，
    而 /ask 会真的去调意图模型 + 检索 + 生成 —— 既慢又依赖外部服务，
    而且它会连带把「embedding 维度不匹配」这类与隔离无关的环境问题
    混进失败原因里（本机就撞过一次：512 vs 集合的 1024）。
    真实链路由 `tests/acceptance_p2_12c.py` 验，那份文件才起真服务。
    """
    uid = repo.get_by_username(username).id
    get_memory_manager().add_exchange(
        session_id, question, f"[{username} 的回答]",
        meta={"intent": "policy_consult", "ts": 1.0, "usage": {}},
        owner_id=uid,
    )


def owner_of(session_id: str) -> str | None:
    """这条会话在库里的归属用户名（None = 无主 / 不存在）。"""
    with db_module.get_engine().connect() as c:
        row = c.execute(
            text("SELECT u.username FROM session s LEFT JOIN user u ON u.id = s.user_id "
                 "WHERE s.id = :sid"),
            {"sid": session_id},
        ).first()
    return row[0] if row else None


def session_row(session_id: str):
    with db_module.get_engine().connect() as c:
        return c.execute(
            text("SELECT id, user_id FROM session WHERE id = :sid"),
            {"sid": session_id},
        ).first()


def message_contents(session_id: str) -> list[str]:
    with db_module.get_engine().connect() as c:
        return [
            r[0] for r in c.execute(
                text("SELECT content FROM chat_message WHERE session_id = :sid ORDER BY seq"),
                {"sid": session_id},
            )
        ]


# --------------------------------------------------------------------------- #
# 前置：账号
# --------------------------------------------------------------------------- #
print("== 第 1 组：前置账号 ==")
_alice = repo.get_by_username(ALICE)
_bob = repo.get_by_username(BOB)
_hr = repo.get_by_username(HR)
check("种子里有这三个账号（两个普通员工 + 一个 hr）",
      all(u is not None for u in (_alice, _bob, _hr)),
      f"{ALICE}={_alice is not None} {BOB}={_bob is not None} {HR}={_hr is not None}")
if not all(u is not None for u in (_alice, _bob, _hr)):
    print("\n  SKIP：种子数据不完整，先跑 make seed-users-apply。")
    sys.exit(1 if os.getenv("REQUIRE_MYSQL") else 0)
check("三个人的 user_id 互不相同（否则隔离无从谈起）",
      len({_alice.id, _bob.id, _hr.id}) == 3,
      f"{_alice.id}/{_bob.id}/{_hr.id}")

sid_a = new_session_id()
sid_b = new_session_id()
sid_hr = new_session_id()
#: Bob 会在第4 组「无主会话认领」里需要一条不属于任何人的会话
sid_orphan = new_session_id()


# --------------------------------------------------------------------------- #
# 第 2 组：写进来的会话真的记在了本人名下
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：归属落库（写的隔离）==")

s, b = write_as(ALICE, sid_a, "报销流程是什么？"), None
check("Alice 写了一句问答（存储层）", True)
check("  └ 库里这条会话的 user_id 是 Alice",
      owner_of(sid_a) == ALICE, f"实际归属 {owner_of(sid_a)}")

write_as(BOB, sid_b, "年假怎么算？")
check("Bob 写了一句问答（存储层）", True)
check("  └ 库里这条会话的 user_id 是 Bob",
      owner_of(sid_b) == BOB, f"实际归属 {owner_of(sid_b)}")
check("  └ 两条会话的 user_id 确实不同",
      session_row(sid_a)[1] != session_row(sid_b)[1],
      f"都是 {session_row(sid_a)[1]}")

#造一条无主会话（12c 之前的历史数据形态：user_id IS NULL）
from core.session_store import SessionSnapshot  # noqa: E402
from core.mysql_store import MySQLSessionStore  # noqa: E402

_mstore = MySQLSessionStore(ttl_seconds=3600)
_mstore.save(
    sid_orphan,
    SessionSnapshot(messages=[{"role": "user", "content": "12c 之前留下的会话"},
                              {"role": "assistant", "content": "旧回答"}]),
    owner_id=None,
)
check("造出来的无主会话 user_id 确实是 NULL",
      session_row(sid_orphan)[1] is None, f"实际 {session_row(sid_orphan)[1]}")


# --------------------------------------------------------------------------- #
# 第 3 组：读隔离（**12c 的核心**）
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：读隔离 ==")

r = client.get("/api/v1/qa/sessions", headers=headers_as(ALICE))
check("Alice 列自己的会话 → 200", r.status_code == 200, r.text[:200])
a_list = {i["session_id"] for i in r.json()}
check("  └ 列表里有自己的会话", sid_a in a_list, f"实际 {sorted(a_list)}")
check("  └ **列表里没有 Bob 的会话**（列表隔离）",
      sid_b not in a_list, f"实际 {sorted(a_list)}")
check("  └ 列表里没有 hr 的会话",
      sid_hr not in a_list and sid_b not in a_list)

r = client.get("/api/v1/qa/sessions", headers=headers_as(BOB))
b_list = {i["session_id"] for i in r.json()}
check("Bob 的列表里有自己的会话", sid_b in b_list, f"实际 {sorted(b_list)}")
check("  └ **Bob 的列表里没有 Alice 的会话**",
      sid_a not in b_list, f"实际 {sorted(b_list)}")

r = client.get(f"/api/v1/qa/sessions/{sid_a}", headers=headers_as(ALICE))
check("Alice 读自己的会话历史 → 200", r.status_code == 200, r.text[:200])
check("  └ 历史里有她自己那句提问",
      any("报销流程" in m["content"] for m in r.json().get("messages", [])),
      f"实际 {r.json().get('messages')}")

r = client.get(f"/api/v1/qa/sessions/{sid_b}", headers=headers_as(ALICE))
check("**Alice 读 Bob 的会话历史 → 403**（不是 404）", r.status_code == 403,
      f"实际 {r.status_code} {r.text[:200]}")
check("  └ 403 的文案不泄漏内容",
      "年假" not in r.text, f"实际 {r.text[:200]}")

r = client.get(f"/api/v1/qa/sessions/{sid_b}", headers=headers_as(HR))
check("**hr 读 Bob 的会话历史 → 403**（管理端权限 ≠ 能看别人的对话）",
      r.status_code == 403, f"实际 {r.status_code} {r.text[:200]}")

r = client.get(f"/api/v1/qa/sessions/{sid_a}", headers=headers_as(BOB))
check("Bob 读 Alice 的会话历史 → 403", r.status_code == 403,
      f"实际 {r.status_code}")

# 读一个不存在的会话：200 + 空列表（不是 403 也不是 404）
# ⚠️ 这里刻意不断言 404：12c 之前就是 200 + 空列表，而「会话不存在」在
# 业务上**不是错误** —— 前端刷新页面时会先拉历史，这时新会话还没发过消息，
# 拉到的就是 200 + 空。改成 404 会让前端每次刷新都报错。
# 与之对照：**越权才是 403**，因为那是一个用户本不该发出的请求。
r = client.get(f"/api/v1/qa/sessions/{PREFIX}ghost", headers=headers_as(ALICE))
check("读一个不存在的会话 → 200 + 空列表（不是错误）",
      r.status_code == 200 and r.json().get("messages") == [],
      f"实际 {r.status_code} {r.text[:200]}")

# 关键：越权读必须**什么都没读到**
r = client.get(f"/api/v1/qa/sessions/{sid_b}", headers=headers_as(ALICE))
check("越权响应体里没有任何消息内容",
      "messages" not in r.json(), f"实际 {r.json()}")


# --------------------------------------------------------------------------- #
# 第 4 组：写隔离 + 无主会话的认领（用户拍板的两条语义）
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：写隔离与无主会话认领 ==")

# ⚠️ /ask 系列的这几条必须真的打接口（越权判定在接口层的预检里），
# 但**不能让它走到 LLM** —— 所以这里用桩链替换 get_rag_chain。
#
# ⚠️ 桩链**必须返回正常响应，不能抛异常**（第一版写成「会炸的」桩，结果是坑）：
#   预检一旦被拆掉，请求就会进到桩链；若桩抛异常，TestClient 会把
#   **服务端的异常重新抛到调用方**（`raise_server_exceptions` 的默认行为），
#   流式那条更是变成 `ExceptionGroup` 直接崩掉整个测试文件 ——
#   于是反向验证看到的是「主测试崩了」，而不是「那条断言转红」。
#   而**崩掉 ≠ 断言红**（第 65 条）：崩掉只说明拆法影响了主测试的运行，
#   说不清是「守卫被拆掉了」还是「改法写错了」。这两者必须能分开。
#   所以桩返回 200：预检被拆 → 断言拿到 200 ≠ 403 → **稳稳地红**。
from api.routes import qa as qa_module  # noqa: E402


class _BenignChain:
    """桩链：不调 LLM，直接返回一个合规的最小响应。"""

    def query(self, question: str, session_id: str, **kwargs) -> dict:
        return {
            "session_id": session_id, "answer": "桩回答", "intent": "chitchat",
            "route": "chitchat", "intent_source": "rule",
            "standalone_question": question, "sources": [], "elapsed_ms": 1.0,
            "usage": {}, "cache_hit": False,
        }

    def stream(self, question: str, session_id: str, **kwargs):
        yield {"type": "meta", "intent": "chitchat", "route": "chitchat",
               "intent_source": "rule", "standalone_question": question, "sources": []}
        yield {"type": "chunk", "content": "桩回答"}
        yield {"type": "done", "elapsed_ms": 1.0, "usage": {}, "session_usage": {}}

    def get_chain_info(self) -> dict:
        return {"stub": True}


_orig_chain = qa_module.get_rag_chain
qa_module.get_rag_chain = lambda: _BenignChain()  # type: ignore[assignment]
try:
    s = client.post(
        "/api/v1/qa/ask",
        json={"question": "Alice 的私密问题", "session_id": sid_b},
        headers=headers_as(ALICE),
    ).status_code
    check("**Alice 往 Bob 的会话里追加提问 → 403**", s == 403, f"实际 {s}")

    s = client.post(
        f"/api/v1/qa/sessions/{sid_b}/truncate",
        json={"keep_messages": 0},
        headers=headers_as(ALICE),
    ).status_code
    check("**Alice 截断 Bob 的会话 → 403**", s == 403, f"实际 {s}")

    s = client.patch(f"/api/v1/qa/sessions/{sid_b}", json={"title": "Alice 改的标题"},
                     headers=headers_as(ALICE)).status_code
    check("**Alice 改名 Bob 的会话 → 403**", s == 403, f"实际 {s}")

    s = client.delete(f"/api/v1/qa/sessions/{sid_b}", headers=headers_as(ALICE)).status_code
    check("**Alice 删 Bob 的会话 → 403**", s == 403, f"实际 {s}")

    s = client.post(
        "/api/v1/qa/ask/stream",
        json={"question": "Alice 越权流式", "session_id": sid_b},
        headers=headers_as(ALICE),
    ).status_code
    check("**流式接口越权 → 403**（而不是流里的一帧 error）", s == 403, f"实际 {s}")
finally:
    qa_module.get_rag_chain = _orig_chain  # type: ignore[assignment]

check("  └ Bob 的会话内容没被污染（仍是 2 条）",
      len(message_contents(sid_b)) == 2, f"实际 {len(message_contents(sid_b))} 条")
check("  └ Bob 会话里没有出现 Alice 那句话",
      not any("私密" in c for c in message_contents(sid_b)),
      f"实际 {message_contents(sid_b)}")
check("  └ 归属也没被改（还是 Bob）", owner_of(sid_b) == BOB, f"实际 {owner_of(sid_b)}")
check("  └ Bob 的会话还在", session_row(sid_b) is not None)

# 合法对照：Bob 往自己的会话里追加，不该是 403
r = client.post(
    "/api/v1/qa/ask",
    json={"question": "我自己追问一句", "session_id": sid_b},
    headers=headers_as(BOB),
)
s = r.status_code
check("**Bob 往自己的会话追问 → 200**（成对断言的另一半）",
      s == 200, f"实际 {s} {r.text[:120]}")

# 认领：无主会话对所有人可见，第一个写入的人把它收归名下
r = client.get(f"/api/v1/qa/sessions/{sid_orphan}", headers=headers_as(ALICE))
check("无主会话对 Alice 可见（认领的前提）", r.status_code == 200, f"实际 {r.status_code}")
check("  └ 看得见旧内容", any("12c 之前" in m["content"] for m in r.json().get("messages", [])),
      f"实际 {r.json().get('messages')}")

write_as(ALICE, sid_orphan, "接着上面那个问题")
check("Alice 在无主会话上追问（存储层写）", True)
check("  └ **无主会话被认领到 Alice 名下**", owner_of(sid_orphan) == ALICE,
      f"实际归属 {owner_of(sid_orphan)}")
check("  └ 旧内容没被覆盖（认领不是重建）",
      any("12c 之前" in c for c in message_contents(sid_orphan)),
      f"实际 {message_contents(sid_orphan)}")
check("  └ 现在变成了 3 轮 = 4 条", len(message_contents(sid_orphan)) == 4,
      f"实际 {len(message_contents(sid_orphan))} 条")
r = client.get(f"/api/v1/qa/sessions/{sid_orphan}", headers=headers_as(BOB))
check("**认领后 Bob 读它 → 403**", r.status_code == 403, f"实际 {r.status_code}")
r = client.get("/api/v1/qa/sessions", headers=headers_as(BOB))
check("  └ 认领后它不在 Bob 的列表里",
      sid_orphan not in {i["session_id"] for i in r.json()})


# --------------------------------------------------------------------------- #
# 第 5 组：无身份头不能读写别人的会话（12b + 12c 的组合）
# --------------------------------------------------------------------------- #
print("\n== 第 5 组：身份回落（dev 模式下的 break-glass） ==")

# ⚠️ 本组在 `IDENTITY_MODE=dev` 下测的是「无身份头**不是**没有身份，
# 而是回落到 IDENTITY_DEV_USERNAME=admin」—— 那是 12b 已定的设计，
# 不是漏洞。所以这里**不断言 401**，而断言回落之后看到的东西仍然受隔离约束。

r = client.get(f"/api/v1/qa/sessions/{sid_a}")
check("无身份头读 Alice 的会话 → 不是 200", r.status_code != 200, f"实际 {r.status_code}")

r = client.get("/api/v1/qa/sessions")
check("无身份头列会话 → 200（dev 模式回落到 break-glass，不是 401）",
      r.status_code == 200, f"实际 {r.status_code} {r.text[:200]}")
_fallback_ids = {i["session_id"] for i in r.json()}
check("  └ **回落之后仍然拿不到任何人的会话**（admin 是 break-glass，id 为 NULL → 只认无主）",
      sid_a not in _fallback_ids and sid_b not in _fallback_ids,
      f"实际 {sorted(_fallback_ids)}")

# gateway 模式下就没有回落了：缺身份头 = 401（12b 的判据，这里钉住不被改回去）
# ⚠️ 直接改 `settings.IDENTITY_MODE` 这个属性，**不要** `importlib.reload`：
#   reload 会换一个 settings 对象，而 `core.identity` 在 import 时就把旧对象
#   绑进了自己的模块命名空间，于是 reload 之后 identity 读到的还是旧模式 ——
#   断言会「因为没切过去而红」，红的理由和它想验的东西无关（第 50 条的假红）。
#   module12 用的就是这个写法，两处保持一致。
_mode_before = settings.IDENTITY_MODE
settings.IDENTITY_MODE = MODE_GATEWAY
try:
    r = client.get("/api/v1/qa/sessions")
    check("gateway 模式下无身份头列会话 → 401（不回落）",
          r.status_code == 401, f"实际 {r.status_code} {r.text[:200]}")
    # Alice 拿自己的证明去读**Bob 的**会话 → 403
    r = client.get(f"/api/v1/qa/sessions/{sid_b}",
                   headers={"X-User-Id": ALICE, "X-Internal-Auth": settings.INTERNAL_SHARED_SECRET})
    check("gateway 模式下带正确证明 → 仍然按归属隔离（Alice 读 Bob → 403）",
          r.status_code == 403, f"实际 {r.status_code}")
    # 同一份证明读自己的会话 → 200（证明没把正常链路也挡住）
    r = client.get(f"/api/v1/qa/sessions/{sid_a}",
                   headers={"X-User-Id": ALICE, "X-Internal-Auth": settings.INTERNAL_SHARED_SECRET})
    check("  └ Alice 在 gateway 模式下读自己的 → 200（证明没把正常链路也挡住）",
          r.status_code == 200, f"实际 {r.status_code}")
finally:
    settings.IDENTITY_MODE = _mode_before


# --------------------------------------------------------------------------- #
# 收尾：清掉本模块造的行 + 断言种子账号一个没少
# --------------------------------------------------------------------------- #
print("\n== 收尾：清理本模块数据 ==")

with db_module.get_engine().begin() as c:
    c.execute(text("DELETE FROM chat_message WHERE session_id LIKE :p"), {"p": f"{PREFIX}%"})
    c.execute(text("DELETE FROM session WHERE id LIKE :p"), {"p": f"{PREFIX}%"})
if AUDIT_BASELINE_MAX_ID is not None:
    with db_module.get_engine().begin() as c:
        c.execute(text("DELETE FROM audit_log WHERE id > :i"), {"i": AUDIT_BASELINE_MAX_ID})

with db_module.get_engine().connect() as c:
    left = c.execute(
        text("SELECT COUNT(*) FROM session WHERE id LIKE :p"), {"p": f"{PREFIX}%"}
    ).scalar()
check(f"本模块造的会话已清干净（session）", left == 0, f"还剩 {left} 条")

left_names = [r[0] for r in db_module.get_engine().connect().execute(
    text("SELECT username FROM user WHERE username IN :names"),
    {"names": ("wu.jing", "zhou.yan", "chen.jie", "zhao.min", "sun.lei",
               "li.na", "zhang.wei", "wang.fang", "xu.hao", "zheng.shuang")}
)]
check("种子十个人一个没少",
      len(set(left_names)) == 10, f"实际 {len(set(left_names))} 个：{sorted(set(left_names))}")

print()
print("=" * 66)
print(f"P2-12c 结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)