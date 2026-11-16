"""rerank API 重试测试（v4.1.0-p2.2 / API 韧性）。

判据：5xx/连接错误重试 2 次（共 3 次尝试，退避 1s/2s）；4xx 不重试；
重试成功救回排序；耗尽后抛出（上层已有「降级为原顺序」兜底）。
"""
import httpx

import core.retriever as rc
from core.retriever import SiliconFlowReranker


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._body


class _FakeClient:
    """按脚本逐次返回响应/抛错；记录调用次数。"""

    calls = 0
    script: list = []

    def __init__(self, *a, **k) -> None: ...
    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *a) -> None: ...

    def post(self, *a, **k) -> _FakeResponse:
        item = type(self).script[min(type(self).calls, len(type(self).script) - 1)]
        type(self).calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _make_reranker(monkeypatch, script, sleeps) -> SiliconFlowReranker:
    monkeypatch.setattr(rc.httpx, "Client", _FakeClient)
    monkeypatch.setattr(rc.time, "sleep", lambda s: sleeps.append(s))
    _FakeClient.calls = 0
    _FakeClient.script = script
    return SiliconFlowReranker(model_name="BAAI/bge-reranker-v2-m3", top_k=3)


def _docs(n: int = 2):
    from langchain_core.documents import Document

    return [Document(page_content=f"内容{i}", metadata={"source": f"f{i}.md"}) for i in range(n)]


OK_BODY = {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.5}]}


def test_502_retried_then_success(monkeypatch):
    sleeps: list[float] = []
    rr = _make_reranker(monkeypatch, [httpx.Response(502).__class__ and _FakeResponse(502), _FakeResponse(200, OK_BODY)], sleeps)
    scored = rr.rerank("q", _docs(), top_k=2)
    assert len(scored) == 2
    assert sleeps == [1.0]  # 第 1 次失败退避 1s，第 2 次成功
    assert _FakeClient.calls == 2


def test_4xx_not_retried(monkeypatch):
    sleeps: list[float] = []
    rr = _make_reranker(monkeypatch, [_FakeResponse(401)], sleeps)
    # 4xx 不重试（calls==1），异常被外层「降级为原顺序」兜底吞掉 → 返回 None
    assert rr.rerank("q", _docs(), top_k=2) is None
    assert _FakeClient.calls == 1 and sleeps == []


def test_retry_exhausted_degrades(monkeypatch):
    """重试耗尽仍 5xx → 内层重抛 → 外层兜底吞掉降级为原顺序（rerank 返回 None）。"""
    sleeps: list[float] = []
    rr = _make_reranker(monkeypatch, [_FakeResponse(502)], sleeps)
    assert rr.rerank("q", _docs(), top_k=2) is None
    assert _FakeClient.calls == 3 and sleeps == [1.0, 2.0]  # 退避 1s/2s
