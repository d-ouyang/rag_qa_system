"""
p0.4c 端到端验证：同一会话连问 3 轮，刷新（重取历史）后每轮引用是否都还在。

对应用户报障现象：
    「我有个会话 #6cdde4e3，只有第一次提问有数据引用，后面都没有」

为什么必须走 HTTP 而不是只跑单元测试：
    那个 bug 的现形条件是「页面刷新 → 走历史接口 → 按 轮号=下标//2 回填元数据」，
    单元测试不经过这条路径，所以三个子版本全绿。这里完整复刻用户操作序列。

运行：
    .venv/bin/python scripts/e2e_p0_4c_meta.py        （需后端已在 :8000 运行）
"""
from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
QUESTIONS = [
    "入职申请具体怎么填写？",
    "试用期转正申请怎么走？",
    "离职手续需要办哪些？",
]


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=180, trust_env=False)
    session_id: str | None = None

    print("=" * 72)
    for i, q in enumerate(QUESTIONS, 1):
        body: dict[str, object] = {"question": q}
        if session_id:
            body["session_id"] = session_id
        resp = client.post("/qa/ask", json=body)
        resp.raise_for_status()
        data = resp.json()
        session_id = data.get("session_id") or session_id
        sources = data.get("sources") or []
        print(
            f"第 {i} 轮 | 即时响应 sources={len(sources)} | intent={data.get('intent')} "
            f"| 引用={[s.get('chunk_id') for s in sources]}"
        )

    if not session_id:
        print("未拿到 session_id，无法继续")
        return 1

    print("=" * 72)
    print(f"session_id = {session_id}")

    # —— 关键一步：模拟「刷新页面」，走历史接口 ——
    hist = client.get(f"/qa/sessions/{session_id}").json()
    items = hist.get("messages") or hist.get("items") or []
    print(f"\n历史接口返回 {len(items)} 条消息")
    print("-" * 72)

    ok = True
    for idx, item in enumerate(items):
        if item.get("role") != "assistant":
            continue
        turn = idx // 2
        srcs = item.get("sources") or []
        ids = [s.get("chunk_id") for s in srcs]
        if not srcs:
            ok = False
        q_text = items[idx - 1].get("content", "")[:20] if idx > 0 else ""
        print(f"{'OK ' if srcs else '!! '}第 {turn + 1} 轮（问：{q_text}…） 引用 {len(srcs)} 条 {ids}")

    print("-" * 72)
    print("结论：", "每一轮刷新后都拿回了自己的引用 ✓" if ok else "仍有轮次丢失引用 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
