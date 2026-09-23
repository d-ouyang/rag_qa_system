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
"""

from __future__ import annotations

from typing import Callable

from core.session_store import SessionSnapshot

#: check(name, condition, detail="") —— 由调用方提供（各测试文件自己的计数与打印）
CheckFn = Callable[..., None]


def exercise_store(store, label: str, check: CheckFn) -> None:
    """
    对任意 SessionStore 实现跑同一套语义断言。

    前置条件：**库里不能有别的会话**。第 114 / 120 / 134 行断言的是
    `list_ids()` 的完整内容与 `stats()["session_count"]` 的精确值，
    所以调用方必须先清空该后端的存储。

    将来加第四个后端（比如 Postgres）直接复用本函数即可。
    """
    # 不存在 → None
    check(f"[{label}] 读不存在的会话返回 None", store.load("nope") is None)
    check(f"[{label}] exists 对不存在返回 False", store.exists("nope") is False)

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
    store.save("s1", snap)

    loaded = store.load("s1")
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
    check(f"[{label}] exists 对存在返回 True", store.exists("s1") is True)

    # 会话列表按活跃倒序（s2 后写，应排在前面）
    store.save("s2", SessionSnapshot(messages=[{"role": "user", "content": "hi"}]))
    check(f"[{label}] list_ids 按活跃倒序", store.list_ids()[:2] == ["s2", "s1"], f"实际 {store.list_ids()}")

    # 删除幂等
    check(f"[{label}] 删除存在的会话返回 True", store.delete("s1") is True)
    check(f"[{label}] 删除不存在的会话返回 False", store.delete("s1") is False)
    check(f"[{label}] 删除后读回 None", store.load("s1") is None)
    check(f"[{label}] 删除后列表只剩一个", store.list_ids() == ["s2"], f"实际 {store.list_ids()}")

    # 覆盖写：同一 session 再存一次，应以最后一次为准（不是追加）
    store.save("s2", SessionSnapshot(messages=[{"role": "user", "content": "第二次"}]))
    again = store.load("s2")
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
