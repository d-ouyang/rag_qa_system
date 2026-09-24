# pyright: basic
"""
模块5测试文件：验证会话记忆（core/memory_manager.py）、意图识别（core/intent_router.py）、
RAG 链（core/rag_chain.py）与问答接口（api/routes/qa.py）

运行：
    .venv/bin/python tests/test_module5_rag_chain_api.py      （或 make test）

覆盖点：
1. 记忆管理：多会话隔离、写入/读取、窗口裁剪、清空、会话列表
2. 意图识别：规则兜底路径（强制禁用 LLM，不发起任何模型调用）
3. RAG 链：LCEL 链能按配置构建出来（不触发真实 LLM 调用）
4. 接口层：TestClient 打健康检查/会话接口；问答接口用桩链替换，验证协议转换

说明：
    · 全程不发起联网请求：意图识别强制走规则路径，问答接口用桩（stub）链替换
    · 真实 LLM 联调请启动服务后手工 curl（见 docs/接口文档-问答API.md）
"""
import sys
import time
from pathlib import Path

# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging

setup_logging()

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """统一的断言输出：通过/失败计数并打印。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


# --------------------------------------------------------------------------- #
# 第 1 组：MemoryManager
# --------------------------------------------------------------------------- #
print("\n== 第 1 组：会话记忆管理 ==")
from core.memory_manager import MemoryManager
from core.session_store import MemorySessionStore

# ⚠️ 必须显式注入进程内存储，不能吃默认后端（2026-09-24 修正）。
# 默认后端按 settings.MEMORY_BACKEND 建 —— 本机 .env 是 mysql，于是这组单元测试
# 会连上**真实业务库**：`会话计数 == 2` 被库里既有的真实会话顶翻（假失败），
# 更糟的是后面 `cleanup_expired()` 会把真实会话一并归档（测试污染业务数据）。
# 单元测试既不该碰业务库，也不该随 .env 漂移。
# （TTL 也要同步下传给 Store，否则 Manager 与 Store 各说各话。）
mm = MemoryManager(max_turns=3, ttl_seconds=3600, store=MemorySessionStore(ttl_seconds=3600))

# 多会话隔离
mm.add_exchange("s1", "问题A1", "回答A1")
mm.add_exchange("s2", "问题B1", "回答B1")
check("会话隔离：s1 只有自己消息", len(mm.get_messages("s1")) == 2)
check("会话隔离：s2 只有自己消息", len(mm.get_messages("s2")) == 2)
check("会话计数正确", mm.session_count() == 2)

# 窗口：库里保留全部轮次，送进模型的只有最近 max_turns 轮
for i in range(2, 5):
    mm.add_exchange("s1", f"问题A{i}", f"回答A{i}")
messages = mm.get_messages("s1")
check("历史全部保留（4 轮 8 条）", len(messages) == 8, f"实际 {len(messages)} 条")
check("最早一轮仍是 A1", messages[0].content == "问题A1", f"实际 {messages[0].content}")
recent = mm.get_recent_messages("s1")
check("模型窗口只取最近 3 轮（6 条）", len(recent) == 6, f"实际 {len(recent)} 条")
check("窗口里最早的一轮是 A2", recent[0].content == "问题A2", f"实际 {recent[0].content}")

# 角色顺序：human/ai 交替
types = [m.type for m in mm.get_messages("s2")]
check("消息角色交替", types == ["human", "ai"], f"实际 {types}")

# 清空会话
check("清空存在的会话返回 True", mm.clear_session("s2") is True)
check("清空不存在的会话返回 False", mm.clear_session("s2") is False)
check("清空后计数减少", mm.session_count() == 1)

# 闲置超过 TTL 仍能读到历史，清理也不会把它删掉
mm_ttl = MemoryManager(max_turns=3, ttl_seconds=0, store=MemorySessionStore(ttl_seconds=0))
mm_ttl.add_exchange("old", "q", "a")
time.sleep(0.01)
check("闲置超 TTL 仍保留历史", mm_ttl.get_messages("old") != [])
check("清理不再删除会话", mm_ttl.cleanup_expired() == 0)
check("清理后历史还在", len(mm_ttl.get_messages("old")) == 2)


# --------------------------------------------------------------------------- #
# 第 2 组：意图识别（规则兜底路径，不调模型）
# --------------------------------------------------------------------------- #
print("\n== 第 2 组：意图识别（规则兜底） ==")
from core.intent_router import Intent, IntentClassifier

classifier = IntentClassifier()
classifier._llm_broken = True      # 强制禁用 LLM，只验证规则映射

cases = [
    # 寒暄整句匹配
    ("你好", Intent.CHITCHAT),
    ("hi", Intent.CHITCHAT),
    ("谢谢", Intent.CHITCHAT),
    ("你是谁？", Intent.CHITCHAT),
    # 寒暄开头但带知识问题：整句不是寒暄，应落知识意图
    ("你好，请问报销流程是什么", Intent.POLICY_CONSULT),
    # 六类知识意图
    ("什么是向量数据库", Intent.KNOWLEDGE_QUERY),
    ("报销流程是什么", Intent.POLICY_CONSULT),        # 报销(政策) 流程(操作) 是什么(知识) 平手 → 政策优先级胜出
    ("怎么配置开发环境", Intent.OPERATION_GUIDE),
    ("A方案和B方案有什么区别", Intent.COMPARISON_ANALYSIS),
    ("上季度销售额统计", Intent.DATA_STATISTICS),
    ("服务报错无法启动", Intent.TROUBLESHOOTING),     # 报错+无法 命中 2 词，主信号胜出
    # 冲突裁决：政策/故障/操作各命中 1 词，故障优先级最高
    ("这个政策执行失败怎么修复", Intent.TROUBLESHOOTING),
    # 兜底默认（安全侧）
    ("随便说点什么", Intent.KNOWLEDGE_QUERY),
]
for question, expected in cases:
    result = classifier.classify(question)
    check(
        f"规则分类：{question!r} -> {expected.value}",
        result.intent is expected and result.source == "rule",
        f"实际 {result.intent.value}/{result.source} ({result.detail})",
    )

# 路由推导：知识意图全部走 rag_qa，闲聊走 chitchat
check("路由：知识意图 -> rag_qa", Intent.TROUBLESHOOTING.route == "rag_qa")
check("路由：闲聊 -> chitchat", Intent.CHITCHAT.route == "chitchat")

empty_result = classifier.classify("   ")
check("空输入按闲聊处理", empty_result.intent is Intent.CHITCHAT)


# --------------------------------------------------------------------------- #
# 第 3 组：RAG 链构建（不触发 LLM 调用）
# --------------------------------------------------------------------------- #
print("\n== 第 3 组：RAG 链构建 ==")
from core.rag_chain import RAGChain, format_docs
from langchain_core.documents import Document

# format_docs 纯函数：不依赖任何模型
docs = [
    Document(page_content="片段一内容", metadata={"source": "/data/a.txt"}),
    Document(page_content="片段二内容", metadata={}),
]
formatted = format_docs(docs)
check("format_docs 带编号", "【资料1】" in formatted and "【资料2】" in formatted)
check("format_docs 带来源", "/data/a.txt" in formatted)
check("format_docs 缺来源兜底", "未知来源" in formatted)

# 链构建：需要向量库连接，但不联网、不调 LLM
chain = RAGChain()
try:
    rag_chain, chitchat_chain = chain._get_chains()
    check("RAG 主链构建成功", rag_chain is not None)
    check("闲聊链构建成功", chitchat_chain is not None)
except Exception as e:
    check("RAG 主链构建成功", False, str(e))
    check("闲聊链构建成功", False, str(e))

# 溯源信息提取
sources = RAGChain._extract_sources([
    Document(page_content="x" * 300, metadata={"source": "/a.txt", "rerank_score": 0.9}),
])
check("溯源提取：来源正确", sources[0]["source"] == "/a.txt")
check("溯源提取：摘要截断到 200 字", len(sources[0]["snippet"]) == 200)
check("溯源提取：分数透传", sources[0]["rerank_score"] == 0.9)

info = chain.get_chain_info()
check("链路信息不含 api_key", "api_key" not in str(info).lower().replace("has_api_key", ""))


# --------------------------------------------------------------------------- #
# 第 4 组：接口层（TestClient，桩链替换，不联网）
# --------------------------------------------------------------------------- #
print("\n== 第 4 组：问答接口 ==")
from fastapi.testclient import TestClient

from api.main import app
from api.routes import qa as qa_module

client = TestClient(app)


class _StubChain:
    """桩链：替换真实 RAGChain，验证接口层的协议转换，不调 LLM。"""

    def query(self, question: str, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "answer": f"桩回答：{question}",
            "intent": "knowledge_query",
            "route": "rag_qa",
            "intent_source": "rule",
            "standalone_question": question,
            "sources": [],
            "elapsed_ms": 1.0,
        }

    def get_chain_info(self) -> dict:
        return {"stub": True}


_original_get_chain = qa_module.get_rag_chain
qa_module.get_rag_chain = lambda: _StubChain()     # type: ignore[assignment]
try:
    # 健康检查
    resp = client.get("/api/v1/qa/health")
    check("GET /health 返回 200", resp.status_code == 200)
    check("health 含 status=ok", resp.json().get("status") == "ok")

    # 同步问答：自动生成 session_id
    resp = client.post("/api/v1/qa/ask", json={"question": "测试问题"})
    check("POST /ask 返回 200", resp.status_code == 200, resp.text[:200])
    body = resp.json()
    check("ask 自动分配 session_id", bool(body.get("session_id")))
    check("ask 回答透传", body["answer"] == "桩回答：测试问题")

    # 参数校验：空问题应 422（pydantic min_length）
    resp = client.post("/api/v1/qa/ask", json={"question": ""})
    check("空问题返回 422", resp.status_code == 422)
finally:
    qa_module.get_rag_chain = _original_get_chain  # type: ignore[assignment]

# 会话管理接口（走真实 MemoryManager，不碰 LLM）
resp = client.get("/api/v1/qa/sessions")
check("GET /sessions 返回 200 且是列表", resp.status_code == 200 and isinstance(resp.json(), list))

resp = client.delete("/api/v1/qa/sessions/not-exist-session")
check("删除不存在会话幂等返回 200", resp.status_code == 200 and resp.json()["cleared"] is False)


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
print(f"\n{'=' * 50}")
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
