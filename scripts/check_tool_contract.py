#!/usr/bin/env python3
"""工具契约静态判据（v4.1.0-p1.1 / Phase ①1c）——「无绕过 registry 的直调」。

规则：检索栈工厂（get_vector_store_manager / get_bm25_index / get_embedding_model /
get_rag_retriever）的 **import 或调用** 只允许出现在：
  · 定义模块自身（vector_store / bm25_index / embedding / retriever）；
  · core/tool_registry.py（唯一收编点）；
  · core/parsing.py、core/bm25_index.py（组件间依赖：parsing→vector_store、bm25→vector_store）；
  · scripts/（评测/运维脚本豁免——不属运行链路）。
业务/评测代码取组件一律 `get_registry().get("组件名")(...)`。违规 → 非零退出（挂 CI 门禁）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTORIES = (
    "get_vector_store_manager",
    "get_bm25_index",
    "get_embedding_model",
    "get_rag_retriever",
)
# 白名单：模块路径（相对 backend 根，不带 .py）→ 允许原因
WHITELIST = {
    "core/tool_registry": "唯一收编点",
    "core/retriever": "retriever 组合内部（构造注入）",
    "core/parsing": "组件间依赖（parse_and_index 用 vector_store）",
}
# 定义模块自身（文件内 def get_xxx 不算直调）
DEFINING = {
    "core/vector_store": "get_vector_store_manager",
    "core/bm25_index": "get_bm25_index",
    "core/embedding": "get_embedding_model",
    "core/retriever": "get_rag_retriever",
}


def main() -> int:
    violations: list[str] = []
    scan_roots = [ROOT / "api", ROOT / "core", ROOT / "models", ROOT / "gateway"]
    for root in scan_roots:
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            rel = py.relative_to(ROOT).with_suffix("").as_posix()
            if any(rel.startswith(w) and (rel == w or rel[len(w) :][:1] in ("", "/")) for w in WHITELIST):
                continue
            if rel in DEFINING:
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for f in FACTORIES:
                    if re.search(rf"\b{f}\b", line):
                        violations.append(f"{rel}.py:{lineno}: {f} —— {line.strip()[:100]}")

    if violations:
        print(f"❌ 工具契约违规 {len(violations)} 处（检索组件工厂必须经 core.tool_registry 取用）：")
        for v in violations:
            print("  ", v)
        print("\n修复：改用 from core.tool_registry import get_registry; get_registry().get('<组件名>')(...)")
        print("确属新的合法白名单场景（组件内部组合）→ 更新本脚本 WHITELIST 并说明原因。")
        return 1
    print("✅ 工具契约检查通过：检索组件工厂无绕过 registry 的直调")
    return 0


if __name__ == "__main__":
    sys.exit(main())
