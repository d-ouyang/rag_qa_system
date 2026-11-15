"""检索栈统一引导（v4.1.0-p1.1 / Phase ①1b）——registry 成为组件唯一入口。

七组件的真实实现工厂在此集中登记（惰性 bootstrap，首次 get_registry() 时注册）：
业务/评测代码一律 `get_registry().get("组件名")(...)` 取用，不再直调各模块的
get_xxx() 工厂——「无绕过 registry 的直调」由 scripts/check_tool_contract.py
静态把关（白名单：定义模块自身、retriever/bm25/parsing 的类内组合、本文件）。

组件间依赖（parsing 用 vector_store、bm25 用 vector_store、retriever 组合全部）
属组件内部实现，不算绕过；对外暴露的入口只有本 registry。
"""
from __future__ import annotations

from functools import lru_cache

from .tool_spec import RagToolRegistry


@lru_cache(maxsize=1)
def get_registry() -> RagToolRegistry:
    """进程级检索栈 registry（惰性 bootstrap；测试可先 reset 再重建）。"""
    from core.bm25_index import get_bm25_index
    from core.embedding import get_embedding_model
    from core.mineru_client import parse_pdf_to_markdown
    from core.parsing import parse_and_index
    from core.retriever import get_rag_retriever
    from core.vector_store import get_vector_store_manager

    reg = RagToolRegistry()
    reg.register("retriever", get_rag_retriever)
    reg.register("vector_store", get_vector_store_manager)
    reg.register("bm25_index", get_bm25_index)
    reg.register("embedding", get_embedding_model)
    reg.register("mineru_client", parse_pdf_to_markdown)
    # parse_and_index 是"文件→切片→入库"的唯一定死入口；改切分参数后的全量
    # 重建就是循环调它——chunk_rebuild 组件指向同一入口（契约上高危、非幂等）。
    reg.register("parsing", parse_and_index)
    reg.register("chunk_rebuild", parse_and_index)
    return reg


def reset_registry() -> None:
    """测试辅助：清空单例（下次 get_registry() 重新 bootstrap）。"""
    get_registry.cache_clear()
