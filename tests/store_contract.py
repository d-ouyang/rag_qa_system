# pyright: basic
"""
会话存储的**共享契约**——同一套断言，所有后端都得过。

--------------------------------------------------------------------------
为什么要把 exercise_store 从 module6 里抽出来
--------------------------------------------------------------------------
`SessionStore` 有 3 个实现（memory / mysql / redis）。如果每个测试文件各写一套
断言，那么「换后端行为一致」这件事就退化成「三份手写断言碰巧写得差不多」——
真正的分歧（比如 mysql 版 list_ids 忘了排除归档会话）不会有任何测试红。
抽成共享函数之后：**实现变了但没改契约 → 一定有人红**。

调用方传自己的 `check` 进来（而不是在这里 import 计数器），是因为各测试文件的
计数与输出格式本来就是它们自己的事；契约只负责「断言什么」。

--------------------------------------------------------------------------
P2-12c：契约里加了归属（ownership）这一组
--------------------------------------------------------------------------
这是 12c 最重要的一个动作。归属语义如果只在每个测试文件里各写一遍，
三个实现仍可能各测各的、漏掉某一种组合；而归属恰恰是「三个实现必须逐条一致」
的东西（memory 用并行字典、mysql 用一列、redis 用 hash field）。

于是把归属断言放进本契约 —— **任何实现少实现一条语义，module6/7/8 一起红**。

⚠️ 本契约用 `owner_id=None` 作为「无主」口径跑第一组（与 12c 之前等价），
    再用两个具体 uid 跑第二组（隔离与认领）。这是刻意的两段：
    只测具体 uid 的话，「owner_id=None 到底什么意思」这条就没被钉住 ——
    而它是12c 里最容易在某个实现上理解歪的一条。
"""

from __future__ import annotations

from typing import Callable

from core.session_store import SessionOwnershipError, SessionSnapshot

#: check(name, condition, detail="") —— 由调用方提供（各测试文件自己的计数与打印）
CheckFn = Callable[..., None]

#: 本契约固定用的两个 uid。它们是**假的**（不对应任何真实员工），
#: 因为契约只验证「归属判定」，不涉及真实账号。挑 440/441 是为了
#: 与真实种子数据形状接近（万一哪天有人忘了隔离测试库会很明显）。
_ALICE = 440
_BOB = 441


def exercise_store(store, label: str, check: CheckFn) -> None:
    """
    对任意 SessionStore 实现跑同一套语义断言（含归属）。

    前置条件：**库里不能有别的会话**。第114 / 120 / 134 行断言的是
    `list_ids()` 的完整内容与 `stats()["session_count"]` 的精确值，
    所以调用方必须先清空该后端的存储。

    将来加第四个后端（比如 Postgres）直接复用本函数即可。
    """
    _exercise_basic(store, label, check)
    _exercise_ownership(store, label, check)


def _exercise_basic(store, label: str, check: CheckFn) -> None:
    """
    第一组：读写/ 列表 / 删除 / 统计的**原有语义**，全部按 owner_id=None 跑。

    为什么用 None 而不是随便一个 uid：None = 无主，是「这条会话不属于任何人」，
    在这个口径下所有读写都应该像 12c 之前一样通畅 —— 它保证 12c 没有
    把「正常会话」本身弄坏。
    """
    u = None
    # 不存在 → None
    check(f"[{label}] 读不存在的会话返回 None", store.load("nope", owner_id=u) is None)
    check(f"[{label}] exists 对不存在返回 False", store.exists("nope", owner_id=u) is False)
    check(
        f"[{label}] 不存在的会话不算 foreign",
        store.is_foreign_to("nope", owner_id=u) is False,
    )

    # 写入 + 读回（含中文与元数据，验证序列化往返）
    snap = SessionSnapshot(
        messages=[
            {"role": "user", "content": "公司的报销流程是什么？"},
            {"role": "assistant", "content": "先提交申请单，再由主管审批。"},
        ],
        exchange_meta=[{"intent": "policy_consult", "elapsed_ms": 123.4}],
        session_meta={"pinned": True, "title": "报销"},
        usage={"input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 0, "requests": 1},
    )
    store.save("s1", snap, owner_id=u)

    loaded = store.load("s1", owner_id=u)
    check(f"[{label}] 写入后能读回", loaded is not None)
    check(f"[{label}] 消息条数与内容一致", loaded is not None and len(loaded.messages) == 2)
    check(
        f"[{label}] 中文内容无乱码",
        loaded is not None and loaded.messages[0]["content"] == "公司的报销流程是什么？",
        f"实际 {loaded.messages[0]['content'] if loaded else None}",
    )
    check(
        f"[{label}] 元数据往返一致",
        loaded is not None and loaded.exchange_meta[0]["intent"] == "policy_consult",
    )
    check(f"[{label}] 置顶标记往返一致", loaded is not None and loaded.session_meta["pinned"] is True)
    check(f"[{label}] 用量往返一致", loaded is not None and loaded.usage["requests"] == 1)
    check(f"[{label}] exists 对存在返回 True", store.exists("s1", owner_id=u) is True)

    # 会话列表按活跃倒序（s2 后写，应排在前面）
    store.save("s2", SessionSnapshot(messages=[{"role": "user", "content": "hi"}]), owner_id=u)
    check(
        f"[{label}] list_ids 按活跃倒序",
        store.list_ids(owner_id=u)[:2] == ["s2", "s1"],
        f"实际 {store.list_ids(owner_id=u)}",
    )

    # 删除幂等
    check(f"[{label}] 删除存在的会话返回 True", store.delete("s1", owner_id=u) is True)
    check(f"[{label}] 删除不存在的会话返回 False", store.delete("s1", owner_id=u) is False)
    check(f"[{label}] 删除后读回 None", store.load("s1", owner_id=u) is None)
    check(f"[{label}] 删除后列表只剩一个", store.list_ids(owner_id=u) == ["s2"], f"实际 {store.list_ids(owner_id=u)}")

    # 覆盖写：同一 session 再存一次，应以最后一次为准（不是追加）
    store.save("s2", SessionSnapshot(messages=[{"role": "user", "content": "第二次"}]), owner_id=u)
    again = store.load("s2", owner_id=u)
    check(
        f"[{label}] save 是整条覆盖（不是追加）",
        again is not None and len(again.messages) == 1 and again.messages[0]["content"] == "第二次",
        f"实际 {again.messages if again else None}",
    )

    # 统计与健康
    stats = store.stats()
    check(f"[{label}] stats 报出后端名", stats.get("backend") == store.name, f"实际 {stats.get('backend')}")
    check(f"[{label}] stats 报出会话数", stats.get("session_count") == 1, f"实际 {stats.get('session_count')}")
    health = store.health()
    check(f"[{label}] health 连通正常", health.get("ok") is True, f"实际 {health}")


def _exercise_ownership(store, label: str, check: CheckFn) -> None:
    """
    第二组：归属（12c 的核心）。三个实现必须逐条一致。

    ⚠️ 前置条件：第一组跑完时库里剩下 `s2`（owner_id=None，无主）。
        所以这里**不依赖「库里空」**，每条都用不同的 sid，避免互相干扰。
    """
    alice, bob = _ALICE, _BOB

    # ---------- 1. 读隔离：别人的会话一律读不到 ----------
    store.save("own-alice", SessionSnapshot(
        messages=[{"role": "user", "content": "Alice 的私密问题"}]
    ), owner_id=alice)

    check(
        f"[{label}] 本人读得到自己的会话",
        store.load("own-alice", owner_id=alice) is not None,
    )
    check(
        f"[{label}] **别人读不到**（读隔离的核心）",
        store.load("own-alice", owner_id=bob) is None,
        "这条红了就是隔离失效：A 能看到 B 的问答记录",
    )
    check(
        f"[{label}] 别人 exists 也是 False",
        store.exists("own-alice", owner_id=bob) is False,
    )
    check(
        f"[{label}] 别人的会话对Bob 是 foreign（用于 403）",
        store.is_foreign_to("own-alice", owner_id=bob) is True,
    )
    check(
        f"[{label}] 本人的会话对本人不是 foreign",
        store.is_foreign_to("own-alice", owner_id=alice) is False,
    )

    # ---------- 2. 列表隔离：只列自己的 ----------
    alice_list = store.list_ids(owner_id=alice)
    bob_list = store.list_ids(owner_id=bob)
    check(
        f"[{label}] 自己的列表里有自己的会话",
        "own-alice" in alice_list,
        f"实际 {alice_list}",
    )
    check(
        f"[{label}] **别人的列表里没有**（列表隔离的核心）",
        "own-alice" not in bob_list,
        f"实际 {bob_list}",
    )
    check(
        f"[{label}] Bob 的列表里有第一组留下的无主会话 s2（认领的前提）",
        "s2" in bob_list,
        f"实际 {bob_list}",
    )

    # ---------- 3. 写隔离：覆盖别人的会话必须被拒 ----------
    def _try_overwrite() -> str:
        """尝试把 Alice 的会话改成 Bob 的内容，返回结果分类。"""
        try:
            store.save("own-alice", SessionSnapshot(
                messages=[{"role": "user", "content": "Bob 篡改"}]
            ), owner_id=bob)
        except SessionOwnershipError:
            return "rejected"
        return "accepted"

    outcome = _try_overwrite()
    check(
        f"[{label}] **覆盖别人的会话抛 SessionOwnershipError**（不是静默接受）",
        outcome == "rejected",
        f"实际 {outcome}（接受 = 越权写入没被拦住）",
    )
    check(
        f"[{label}] 被拒后原内容没被改",
        (store.load("own-alice", owner_id=alice) or SessionSnapshot()).messages
        == [{"role": "user", "content": "Alice 的私密问题"}],
        "越权写入即使报错了也不该改动对方的会话",
    )

    # ---------- 4. 删除隔离：删不掉别人的 ----------
    check(
        f"[{label}] **删别人的会话返回 False**",
        store.delete("own-alice", owner_id=bob) is False,
    )
    check(
        f"[{label}] 别人的会话被删后仍然存在",
        store.load("own-alice", owner_id=alice) is not None,
        "越权删除把Alice 的会话真删掉了",
    )
    check(
        f"[{label}] 本人删自己的返回 True",
        store.delete("own-alice", owner_id=alice) is True,
    )

    # ---------- 5. 无主会话的认领（用户拍板的语义）----------
    check(
        f"[{label}] 无主会话对登录用户可见（认领的前提）",
        store.load("s2", owner_id=bob) is not None,
        "读不到就无从认领：用户会以为会话不存在，然后从零重建并覆盖原内容",
    )
    store.save("s2", SessionSnapshot(
        messages=[{"role": "user", "content": "Bob 认领后写的"}]
    ), owner_id=bob)
    check(
        f"[{label}] 无主会话被认领后归新主人",
        store.load("s2", owner_id=bob) is not None
        and store.load("s2", owner_id=alice) is None,
        f"认领后 Alice 仍能读到：实际 {store.load('s2', owner_id=alice)}",
    )
    check(
        f"[{label}] 认领后 Alice 视角下它变foreign",
        store.is_foreign_to("s2", owner_id=alice) is True,
    )
    check(
        f"[{label}] 认领后 Alice 列表里不再有它",
        "s2" not in store.list_ids(owner_id=alice),
        f"实际 {store.list_ids(owner_id=alice)}",
    )

    # ---------- 6. owner_id=None 的语义：只认无主 ----------
    store.save("own-alice2", SessionSnapshot(
        messages=[{"role": "user", "content": "Alice 第二条"}]
    ), owner_id=alice)
    check(
        f"[{label}] owner_id=None **读不到有主的会话**（不是「不过滤」）",
        store.load("own-alice2", owner_id=None) is None,
        "这条红 = None 被实现成了「列出全部」，那是泄漏",
    )
    check(
        f"[{label}] owner_id=None 的列表里没有有主的会话",
        "own-alice2" not in store.list_ids(owner_id=None),
        f"实际 {store.list_ids(owner_id=None)}",
    )
    check(
        f"[{label}] owner_id=None 也读不到有主的会话的 exists",
        store.exists("own-alice2", owner_id=None) is False,
    )

    # 收尾：把这一组建的会话清掉，别影响调用方后续的精确计数断言
    store.delete("own-alice2", owner_id=alice)
    store.delete("s2", owner_id=bob)