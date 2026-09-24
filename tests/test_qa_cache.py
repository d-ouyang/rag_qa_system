# pyright: basic
"""相同问题缓存：规范化、最近使用淘汰、知识库版本失效。不连真实 Redis。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fakeredis

from config.settings import settings
from core.qa_cache import (
    bump_kb_version,
    is_standalone_question,
    lookup_answer,
    normalize_question,
    store_answer,
)

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ok  {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}  {detail}")


settings.QA_CACHE_ENABLED = True
settings.QA_CACHE_MAX_ENTRIES = 2
client = fakeredis.FakeRedis(decode_responses=False)

q1 = "员工如何办理居住证？"
q1_same = "  员工如何办理居住证?  "
q2 = "办理居住证的流程是什么？"
q3 = "年假有几天？"

check("问号和空白被折成同一句", normalize_question(q1) == normalize_question(q1_same))
check("近义句不会折成同一句", normalize_question(q1) != normalize_question(q2))
check("完整问句可以写入公共缓存", is_standalone_question(q1))
check("指代句不写入公共缓存", not is_standalone_question("它呢"))

store_answer(q1, "带材料去窗口", [{"index": 1, "source": "/a"}], "policy_consult", "rag_qa", q1, client=client)
hit = lookup_answer(q1_same, client=client)
check("相同问题命中", hit is not None and hit["answer"] == "带材料去窗口", str(hit))
check("命中次数加一", hit is not None and hit["hit_count"] == 1)
check("近义问题不命中", lookup_answer(q2, client=client) is None)

store_answer(q2, "另一套流程", [{"index": 1}], "policy_consult", "rag_qa", q2, client=client)
# q2 比 q1 新。命中 q1 后它变成最近使用，再写入 q3 应淘汰 q2。
check("写入后 q1 仍可命中", lookup_answer(q1, client=client) is not None)
store_answer(q3, "五天", [{"index": 1}], "policy_consult", "rag_qa", q3, client=client)
check("最近使用的 q1 还在", lookup_answer(q1, client=client) is not None)
check("最久未用的 q2 被淘汰", lookup_answer(q2, client=client) is None)
check("新写入的 q3 在池子里", lookup_answer(q3, client=client) is not None)

bumped = bump_kb_version(client=client)
check("版本递增", bumped == 2, str(bumped))
check("版本变化后旧答案不再命中", lookup_answer(q1, client=client) is None)
check("版本变化后池子是空的", lookup_answer(q3, client=client) is None)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
