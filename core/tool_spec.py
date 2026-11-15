"""检索工具契约（v4.1.0-p1.0 / Phase ①1）——与 office 2.2.0 的 ToolSpec 同源（S3）。

定位：RAG 是**带治理的固定管线**，不是 Agent——这里的"工具"是检索链路的组件
（retriever / vector_store / bm25_index / embedding / mineru_client / parsing），
登记的目的是让链路每一步**可枚举、可批注横切元数据、可注入 Fake**，为：
  · 权限/审计分级（risk_level / permission）
  · 幂等去重与副作用声明（side_effect / idempotent）
  · 分级超时（timeout）
  · 评测 Fake 注入（override）
提供单一契约面。无绕过 registry 的直调是 ①1 完成判据（调用点收编在后续 p1.x）。

缺省值全部悲观（side_effect=True / idempotent=False）：漏登记的组件按最危险
的一档处理——与 office 的 unannotated() 抓漏机制同款。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    """检索组件的横切元数据契约（与 office 2.2.0-p1.1 同源，字段语义一致）。

    - risk_level：low/medium/high——审计分级与高危操作拦截的依据；
    - side_effect：是否有写副作用（文件/DB/索引重建）；
    - idempotent：**同参数重放是否安全**（重试/重放场景的护栏依据）；
    - timeout：该组件的分级超时秒数（声明面）；
    - permission：`域:动作` 权限语义（rag:query / db:read / fs:read / net:call / admin:rebuild）。
    """

    risk_level: str = "medium"
    side_effect: bool = True
    idempotent: bool = False
    timeout: float | None = None
    permission: str = ""


#: 检索栈组件的集中批注（一处可审计）。改切分=整库重建是本项目最高危操作，
#: chunk_rebuild 显式标 high/非幂等（呼应 ①2 的风险标记要求）。
RETRIEVAL_TOOL_SPECS: dict[str, ToolSpec] = {
    "retriever": ToolSpec(risk_level="low", side_effect=False, idempotent=True, timeout=60, permission="rag:query"),
    "vector_store": ToolSpec(risk_level="low", side_effect=False, idempotent=True, timeout=30, permission="db:read"),
    "bm25_index": ToolSpec(risk_level="low", side_effect=False, idempotent=True, timeout=30, permission="fs:read"),
    "embedding": ToolSpec(risk_level="low", side_effect=False, idempotent=True, timeout=60, permission="net:call"),
    "mineru_client": ToolSpec(risk_level="medium", side_effect=False, idempotent=True, timeout=120, permission="net:call"),
    "parsing": ToolSpec(risk_level="low", side_effect=False, idempotent=True, timeout=60, permission="fs:read"),
    # 高危显式标记：切分参数改动 = 整库重建（①2 完成判据的契约面）
    "chunk_rebuild": ToolSpec(risk_level="high", side_effect=True, idempotent=False, timeout=None, permission="admin:rebuild"),
}

#: 显式批注缺省值之前，未登记组件的兜底（悲观档）。
_PESSIMISTIC_SPEC = ToolSpec()


class RagToolRegistry:
    """检索栈组件登记表：名字 → (可调用实现, ToolSpec)。

    生产代码从这里取组件实现（后续 p1.x 收编直调点），测试用 override 注入 Fake。
    """

    def __init__(self) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}
        self._specs: dict[str, ToolSpec] = {}

    def register(self, name: str, fn: Callable[..., Any], spec: ToolSpec | None = None) -> None:
        """登记组件；spec 缺省用集中批注表，没有批注的落悲观档。"""
        self._fns[name] = fn
        self._specs[name] = spec or RETRIEVAL_TOOL_SPECS.get(name) or _PESSIMISTIC_SPEC

    def override(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """测试注入口：替换组件实现为 Fake，返回原实现便于恢复。"""
        if name not in self._fns:
            raise KeyError(f"override：组件 {name} 未登记")
        old = self._fns[name]
        self._fns[name] = fn
        return old

    def get(self, name: str) -> Callable[..., Any]:
        if name not in self._fns:
            raise KeyError(f"get：组件 {name} 未登记")
        return self._fns[name]

    def has(self, name: str) -> bool:
        return name in self._fns

    def spec(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise KeyError(f"spec：组件 {name} 未登记")
        return self._specs[name]

    def all_names(self) -> list[str]:
        return sorted(self._fns)

    def unannotated(self) -> list[str]:
        """登记了实现但用的是悲观兜底 spec 的组件（测试抓漏：应为空）。"""
        return sorted(n for n, s in self._specs.items() if s is _PESSIMISTIC_SPEC)
