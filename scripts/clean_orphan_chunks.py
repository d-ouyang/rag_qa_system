"""清理 Chroma 里的孤儿切片。

孤儿 = doc_id 不在 document 表中的切片。成因：删除文档与后台解析并发时，
worker 在删除动作之后才把切片写入 Chroma，文档行已不存在、切片却残留。

用法（服务器）：
    docker cp /tmp/clean_orphan_chunks.py rag-backend:/tmp/clean.py
    docker exec -w /app rag-backend python /tmp/clean.py            # 干跑：只报告不删
    docker exec -w /app rag-backend python /tmp/clean.py --apply    # 真删

2026-10-09 首次使用背景：服务器首部署验收期间，test.md/test1.md 在解析中
被删除，残留 6 个孤儿切片（来源 /app/upload/<hash>.md 两个）。
"""

import os
import sys

import chromadb
from sqlalchemy import text

from core.db import get_engine

APPLY = "--apply" in sys.argv


def main() -> int:
    engine = get_engine()
    client = chromadb.HttpClient(
        host=os.environ.get("CHROMA_HOST", "chroma"),
        port=int(os.environ.get("CHROMA_PORT", "8000")),
    )
    col = client.get_collection("rag_qa_knowledge")

    with engine.connect() as conn:
        valid = {str(r[0]) for r in conn.execute(text("SELECT id FROM document"))}
    print(f"document 表现有 {len(valid)} 行")

    d = col.get(include=["metadatas"])
    print(f"Chroma 切片总数 {len(d['ids'])}")
    bad: list[str] = []
    for i, m in zip(d["ids"], d["metadatas"]):
        doc_id = m.get("doc_id")
        ok = str(doc_id) in valid
        if not ok:
            bad.append(i)
        print(f"  {'OK    ' if ok else 'ORPHAN'} {i[:8]} doc_id={doc_id!r} source={m.get('source')}")

    print(f"孤儿切片 {len(bad)} 个")
    if not bad:
        print("无需清理。")
        return 0
    if not APPLY:
        print("干跑结束；确认上面 ORPHAN 清单无误后，加 --apply 真删。")
        return 0
    col.delete(ids=bad)
    print(f"已删除 {len(bad)} 个孤儿切片，剩余向量数 {col.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
