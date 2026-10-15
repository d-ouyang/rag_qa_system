#!/usr/bin/env python3
"""
整库重建前的切片正文快照（P3-7a 一次性脚本）。

把当前库里每个 chunk_id 的「给人看的原文」导成 {chunk_id: text}，
供 scripts/remap_golden_p37a.py 在重建后按内容重映射 golden 标注。

    .venv/bin/python scripts/snapshot_kb_p37a.py --out /tmp/kb_snapshot_fixed.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.vector_store import get_vector_store_manager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    store = get_vector_store_manager()
    snapshot: dict[str, str] = {}
    for item in store.get_all_chunks():
        meta = item.get("metadata") or {}
        original = meta.get("content")
        # fixed 模式（老库）没有 metadata.content：存储正文就是原文
        snapshot[item["chunk_id"]] = str(original) if original else item["text"]
    args.out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"快照 {len(snapshot)} 条切片 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
