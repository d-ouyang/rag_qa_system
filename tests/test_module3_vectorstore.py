# pyright: basic
"""
模块3测试文件：验证嵌入模型加载（core/embedding.py）与向量库操作（core/vector_store.py）

运行：
    .venv/bin/python tests/test_module3_vectorstore.py      （或 make test）

覆盖点：
1. 嵌入模型单例：多次实例化是同一对象，不会重复加载
2. 嵌入输出：维度、归一化（L2 范数≈1）、批量、语义相近度
3. 向量库 CRUD：初始化 → 新增 → 检索 / 带分数检索 / 元数据过滤 → 按来源删除
4. 持久化：重新打开同一目录数据仍在（Chroma 自动落盘、FAISS 显式 save_local 落盘）
5. 状态元数据 get_stats()
6. 清空 clear_all()
7. 边界：空库检索、空列表写入、删除不存在的来源、非法后端类型、管理器单例

说明：全部用例都写在临时目录里（tempfile），**不会污染真实的 vector_db/**。
      其中「管理器单例」那一组会读写 settings 里的真实目录，仅用于验证单例行为，不写入业务数据。
"""
import sys
import tempfile
from pathlib import Path

# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.documents import Document

from config.logging_config import setup_logging
from config.settings import settings
from core.embedding import EmbeddingModel, get_embedding_model, get_embedding_model_info
from core.vector_store import (
    VectorStoreManager,
    get_vector_store_manager,
    reset_vector_store_manager,
)

# 初始化日志
setup_logging()

# 两种后端都要验证
BACKENDS = ("chroma", "faiss")

# 测试用的固定来源：刻意用两个不同文件，便于验证「按来源删除」与元数据过滤
SOURCE_A = "/tmp/test_docs/考勤制度.txt"
SOURCE_B = "/tmp/test_docs/报销制度.txt"

# 测试语料：3 个片段、2 个来源（SOURCE_A 2 条，SOURCE_B 1 条）
TEST_DOCS: list[Document] = [
    Document(
        page_content="员工入职满一年可享受5天带薪年假，满十年可享受10天年假。",
        metadata={"source": SOURCE_A, "file_name": "考勤制度.txt", "file_type": "txt"},
    ),
    Document(
        page_content="请假需提前一天在办公系统提交申请，并经直属主管审批通过后方可休假。",
        metadata={"source": SOURCE_A, "file_name": "考勤制度.txt", "file_type": "txt"},
    ),
    Document(
        page_content="差旅费报销需在行程结束后7个工作日内提交发票与审批单。",
        metadata={"source": SOURCE_B, "file_name": "报销制度.txt", "file_type": "txt"},
    ),
]

# 逐条断言的结果统计
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果，避免第一个失败就中断后续用例。"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name}（{detail}）" if detail else name)
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def _dot(vec_a: list[float], vec_b: list[float]) -> float:
    """归一化向量的点积就是余弦相似度。"""
    return sum(i * j for i, j in zip(vec_a, vec_b))


def _no_raise(func) -> bool:  # type: ignore[no-untyped-def]
    """执行一个无参函数，返回是否没有抛异常。"""
    try:
        func()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 1. 嵌入模型单例
# --------------------------------------------------------------------------- #
def test_embedding_singleton() -> None:
    print("\n==== 1. 嵌入模型单例 ====")

    first = EmbeddingModel()
    second = EmbeddingModel()
    check("两次实例化得到同一对象", condition=first is second)
    check("get_embedding_model 两次拿到同一模型", get_embedding_model() is get_embedding_model())
    check("模型对象非空", get_embedding_model() is not None)

    info = get_embedding_model_info()
    print(f"  模型信息：{info}")
    check("记录了加载来源（local / remote）", info["loaded_from"] in ("local", "remote"), str(info["loaded_from"]))
    check("向量维度为 512（bge-small-zh）", info["dimension"] == 512, str(info["dimension"]))
    check("归一化开关已开启", info["normalize_embeddings"] is True)
    check("运行设备来自 settings", info["device"] == settings.EMBEDDING_DEVICE, str(info["device"]))


# --------------------------------------------------------------------------- #
# 2. 嵌入输出
# --------------------------------------------------------------------------- #
def test_embedding_output() -> None:
    print("\n==== 2. 嵌入输出 ====")
    model = get_embedding_model()

    vec = model.embed_query("年假可以休几天")
    check("embed_query 返回 512 维向量", len(vec) == 512, str(len(vec)))

    # 归一化是检索打分可比的前提：L2 范数应约等于 1
    norm = sum(x * x for x in vec) ** 0.5
    check("向量已归一化（L2 范数≈1）", abs(norm - 1.0) < 1e-4, f"{norm:.6f}")

    batch = model.embed_documents(["第一句话", "第二句话", "第三句话"])
    check("embed_documents 批量返回条数正确", len(batch) == 3, str(len(batch)))
    check("批量结果维度一致", all(len(v) == 512 for v in batch))

    # 语义验证：同一句话的相似度必须高于无关句，否则说明模型没真正生效
    same_a = model.embed_query("年假天数怎么计算")
    same_b = model.embed_query("年假天数怎么计算")
    other = model.embed_query("今天天气真不错")
    check(
        "同句相似度高于无关句",
        _dot(same_a, same_b) > _dot(same_a, other),
        f"{_dot(same_a, same_b):.4f} vs {_dot(same_a, other):.4f}",
    )


# --------------------------------------------------------------------------- #
# 3. 向量库 CRUD + 状态 + 清空
# --------------------------------------------------------------------------- #
def _run_crud_suite(store_type: str, work_dir: Path) -> None:
    """对指定后端跑完整流程：初始化 → 新增 → 检索 → 过滤 → 状态 → 删除 → 清空。"""
    tag = f"[{store_type}]"
    manager = VectorStoreManager(store_type=store_type, persist_dir=work_dir)
    check(f"{tag} 新库初始为空", manager.count() == 0, f"count={manager.count()}")

    # ---------------- 新增 ----------------
    added = manager.add_documents(TEST_DOCS)
    check(f"{tag} 新增返回条数正确", added == len(TEST_DOCS), f"{added} vs {len(TEST_DOCS)}")
    check(f"{tag} 新增后总数正确", manager.count() == len(TEST_DOCS), f"count={manager.count()}")
    check(f"{tag} 写入空列表返回 0", manager.add_documents([]) == 0)

    # ---------------- 检索（只做向量召回，不接大模型）----------------
    results = manager.similarity_search("年假可以休几天", k=2)
    check(f"{tag} 检索有返回", len(results) > 0, f"{len(results)} 条")
    check(f"{tag} 返回的是 Document 对象", all(isinstance(d, Document) for d in results))
    check(
        f"{tag} 检索结果保留完整元数据（source/file_name/file_type 不丢）",
        bool(results) and all({"source", "file_name", "file_type"} <= set(d.metadata) for d in results),
        str(results[0].metadata) if results else "空结果",
    )
    top_content = results[0].page_content if results else ""
    check(f"{tag} 检索命中语义最相关片段", "年假" in top_content, top_content[:40])

    scored = manager.similarity_search_with_score("差旅费怎么报销", k=2)
    check(
        f"{tag} 带分数检索返回 (Document, 分数)",
        bool(scored) and isinstance(scored[0][0], Document) and isinstance(scored[0][1], float),
        str(scored[0]) if scored else "空结果",
    )
    # 必须是 Python 原生 float：FAISS 原生返回 numpy.float32，
    # 若不统一转换，将来 API 直接返回分数会报「float32 is not JSON serializable」
    check(
        f"{tag} 分数是原生 Python float（不是 numpy.float32）",
        bool(scored) and type(scored[0][1]) is float,
        str(type(scored[0][1])),
    )
    if len(scored) >= 2:
        check(
            f"{tag} 分数升序（越小越相似）",
            scored[0][1] <= scored[1][1],
            f"{scored[0][1]:.4f} vs {scored[1][1]:.4f}",
        )

    # ---------------- 元数据过滤 ----------------
    filtered = manager.similarity_search("制度", k=5, filter_dict={"source": SOURCE_B})
    check(
        f"{tag} 元数据过滤只返回指定来源",
        bool(filtered) and all(d.metadata.get("source") == SOURCE_B for d in filtered),
        str([d.metadata.get("source") for d in filtered]),
    )

    # ---------------- 状态元数据 ----------------
    stats = manager.get_stats()
    check(f"{tag} 状态含后端类型", stats["vector_store_type"] == store_type, str(stats["vector_store_type"]))
    check(f"{tag} 状态片段总数正确", stats["total_vectors"] == len(TEST_DOCS), str(stats["total_vectors"]))
    check(f"{tag} 状态文件数按 source 去重", stats["source_count"] == 2, str(stats["sources"]))
    check(f"{tag} 状态含全部来源路径", set(stats["sources"]) == {SOURCE_A, SOURCE_B}, str(stats["sources"]))
    check(f"{tag} 状态含嵌入模型信息", stats["embedding_dimension"] == 512, str(stats["embedding_dimension"]))
    check(f"{tag} 状态含持久化目录", stats["persist_directory"] == str(work_dir), str(stats["persist_directory"]))
    if store_type == "chroma":
        check(f"{tag} 状态含 collection 名", stats["collection_name"] == "rag_qa_knowledge", str(stats["collection_name"]))
    else:
        check(f"{tag} FAISS 无 collection 概念（返回 None）", stats["collection_name"] is None, str(stats["collection_name"]))

    # ---------------- 按来源删除 ----------------
    deleted = manager.delete_by_source(SOURCE_A)
    check(f"{tag} 按来源删除条数正确（该来源有 2 条）", deleted == 2, f"deleted={deleted}")
    check(f"{tag} 删除后总数减少", manager.count() == len(TEST_DOCS) - 2, f"count={manager.count()}")
    left = manager.similarity_search("年假", k=5)
    check(
        f"{tag} 已删除来源不再被召回",
        all(d.metadata.get("source") != SOURCE_A for d in left),
        str([d.metadata.get("source") for d in left]),
    )
    check(f"{tag} 删除不存在的来源返回 0", manager.delete_by_source("/nowhere/不存在.txt") == 0)
    check(f"{tag} 删除后状态同步更新", manager.get_stats()["source_count"] == 1)

    # ---------------- 清空 ----------------
    manager.clear_all()
    check(f"{tag} 清空后总数为 0", manager.count() == 0, f"count={manager.count()}")
    check(f"{tag} 清空后检索返回空", manager.similarity_search("年假", k=3) == [])
    check(f"{tag} 清空后状态里来源清零", manager.get_stats()["source_count"] == 0)
    check(f"{tag} 清空后仍可继续写入", manager.add_documents(TEST_DOCS[:1]) == 1)
    check(f"{tag} 清空后写入的数据可检索", manager.similarity_search("年假", k=1) != [])


# --------------------------------------------------------------------------- #
# 4. 持久化（本组是 FAISS「必须及时更新索引」的关键验证）
# --------------------------------------------------------------------------- #
def _run_persistence_suite(store_type: str, work_dir: Path) -> None:
    """写入后重新打开同一目录，数据必须还在。"""
    tag = f"[{store_type}]"

    writer = VectorStoreManager(store_type=store_type, persist_dir=work_dir)
    writer.add_documents(TEST_DOCS)
    check(f"{tag} 写入后总数正确", writer.count() == len(TEST_DOCS), f"count={writer.count()}")

    # 关键：重新构造一个管理器，模拟「进程重启」
    reopened = VectorStoreManager(store_type=store_type, persist_dir=work_dir)
    check(f"{tag} 重新打开后数据仍在", reopened.count() == len(TEST_DOCS), f"count={reopened.count()}")
    results = reopened.similarity_search("年假可以休几天", k=1)
    check(
        f"{tag} 重新打开后仍能检索到正确片段",
        bool(results) and "年假" in results[0].page_content,
        results[0].page_content[:30] if results else "无结果",
    )

    # 删除后也要落盘，否则「删掉的数据会在重启后复活」
    reopened.delete_by_source(SOURCE_A)
    third = VectorStoreManager(store_type=store_type, persist_dir=work_dir)
    check(f"{tag} 删除结果同样持久化", third.count() == len(TEST_DOCS) - 2, f"count={third.count()}")

    # 清空后重开，也要确认真的清干净了
    third.clear_all()
    fourth = VectorStoreManager(store_type=store_type, persist_dir=work_dir)
    check(f"{tag} 清空结果同样持久化", fourth.count() == 0, f"count={fourth.count()}")


# --------------------------------------------------------------------------- #
# 5. 边界与异常
# --------------------------------------------------------------------------- #
def test_empty_store_and_errors() -> None:
    print("\n==== 5. 边界与异常 ====")

    for store_type in BACKENDS:
        with tempfile.TemporaryDirectory() as tmp:
            tag = f"[{store_type}]"
            manager = VectorStoreManager(store_type=store_type, persist_dir=Path(tmp))
            check(f"{tag} 空库 count=0", manager.count() == 0)
            check(f"{tag} 空库检索返回空列表（不抛异常）", manager.similarity_search("任意问题") == [])
            check(f"{tag} 空库带分数检索返回空列表", manager.similarity_search_with_score("任意问题") == [])
            empty_stats = manager.get_stats()
            check(
                f"{tag} 空库状态字段全为 0/空",
                empty_stats["total_vectors"] == 0
                and empty_stats["source_count"] == 0
                and empty_stats["sources"] == [],
                str(empty_stats["sources"]),
            )
            check(f"{tag} 空库删除返回 0", manager.delete_by_source(SOURCE_A) == 0)
            check(f"{tag} 空库清空不报错", _no_raise(manager.clear_all))

    # 非法后端类型要尽早报错，而不是等到写入时才失败
    with tempfile.TemporaryDirectory() as tmp:
        try:
            VectorStoreManager(store_type="milvus", persist_dir=Path(tmp))
            check("非法后端类型抛 ValueError", False, "没有抛异常")
        except ValueError:
            check("非法后端类型抛 ValueError", True)
        except Exception as e:
            check("非法后端类型抛 ValueError", False, f"抛的是 {type(e).__name__}")


# --------------------------------------------------------------------------- #
# 6. 管理器单例
# --------------------------------------------------------------------------- #
def test_manager_singleton() -> None:
    print("\n==== 6. 向量库管理器单例 ====")
    reset_vector_store_manager()

    first = get_vector_store_manager()
    second = get_vector_store_manager()
    check("两次获取是同一对象", first is second)
    check(
        "单例使用 settings 里的后端类型",
        first.store_type == settings.VECTOR_STORE_TYPE,
        f"{first.store_type} vs {settings.VECTOR_STORE_TYPE}",
    )
    check("单例使用 settings 里的持久化目录", first.persist_dir == settings.VECTOR_DB_DIR, str(first.persist_dir))

    reset_vector_store_manager()
    third = get_vector_store_manager()
    check("重置后会按配置重建新对象", third is not first)
    reset_vector_store_manager()


def main() -> int:
    # 前置检查：嵌入模型必须可用，否则后面所有用例都没有意义
    print("==== 0. 前置检查：嵌入模型 ====")
    try:
        check("嵌入模型可加载", get_embedding_model() is not None)
    except Exception as e:
        print(f"  [FATAL] 嵌入模型加载失败，后续用例无法执行：{e}")
        return 1

    test_embedding_singleton()
    test_embedding_output()

    print("\n==== 3. 向量库 CRUD + 状态 + 清空（chroma / faiss 双后端） ====")
    for store_type in BACKENDS:
        with tempfile.TemporaryDirectory() as tmp:
            _run_crud_suite(store_type, Path(tmp))

    print("\n==== 4. 持久化（chroma / faiss 双后端） ====")
    for store_type in BACKENDS:
        with tempfile.TemporaryDirectory() as tmp:
            _run_persistence_suite(store_type, Path(tmp))

    test_empty_store_and_errors()
    test_manager_singleton()

    print(f"\n==== 汇总：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项 ====")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
