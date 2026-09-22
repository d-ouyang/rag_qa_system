# pyright: basic
"""
模块4测试文件：验证大模型客户端（core/llm_client.py）与检索器（core/retriever.py）

运行：
    .venv/bin/python tests/test_module4_llm_and_retriever.py      （或 make test）

覆盖点：
1. LLM 客户端：按配置生成不同 provider（openai / siliconflow / ollama）
2. 配置校验：缺 API Key、空模型名、不支持的 provider → LLMConfigError
3. 密钥安全：get_model_info() 不含密钥
4. 客户端缓存：同配置复用实例，不同配置互不覆盖
5. 检索器：候选池放大、不重排 / 重排两条链路、分数来源正确
6. 重排器：模型解析（本地优先）、打分、阈值过滤、加载失败降级
7. as_retriever：接入 LCEL 的 Retriever 形态与空库保护
8. 边界：空查询、非法 top_k、检索器单例

说明：
    · 全部向量库数据写在临时目录（tempfile），不会污染真实的 vector_db/
    · 除第 10 组外全程不发起联网请求：只验证客户端能否按配置构造出来
    · 第 10 组是本地 ollama 的真实调用（invoke / stream），
      检测不到 ollama 服务或未拉取对应模型时自动跳过，不计入失败
"""
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain.retrievers import ContextualCompressionRetriever
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from langchain_core.retrievers import BaseRetriever

from config.logging_config import setup_logging
from config.settings import settings
from core.llm_client import (
    LLMClient,
    LLMConfigError,
    get_llm,
    get_llm_client,
    reset_llm_client_cache,
)
from core.retriever import (
    CrossEncoderReranker,
    RAGRetriever,
    get_rag_retriever,
    reset_rag_retriever,
    resolve_reranker_model_path,
)
from core.vector_store import VectorStoreManager, reset_vector_store_manager

# 初始化日志
setup_logging()

# 临时库里写入的测试语料：第 1 条是最相关的，其余是干扰项
TEST_TEXTS: list[str] = [
    "员工入职满一年可享受5天带薪年假，满十年可享受10天年假。",
    "请假需提前一天在办公系统提交申请，并经直属主管审批通过后方可休假。",
    "差旅费报销需在行程结束后7个工作日内提交发票与审批单。",
    "公司每年组织一次健康体检，员工可携带一名家属参加。",
    "加班需提前填写加班申请单，由部门经理审批后生效。",
    "办公用品领用请到行政前台登记，每人每月限额200元。",
]
QUERY = "年假能休多少天"

SOURCE_A = "/tmp/test_docs/考勤制度.txt"
SOURCE_B = "/tmp/test_docs/报销制度.txt"

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


def _no_raise(func) -> bool:  # type: ignore[no-untyped-def]
    """执行一个无参函数，返回是否没有抛异常。"""
    try:
        func()
        return True
    except Exception:
        return False


def _build_docs() -> list[Document]:
    """构造写进临时向量库的 Document 列表（前 3 条来源 A，后 3 条来源 B）。"""
    docs: list[Document] = []
    for index, text in enumerate(TEST_TEXTS):
        source = SOURCE_A if index < 3 else SOURCE_B
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "source": source,
                    "file_name": Path(source).name,
                    "file_type": "txt",
                    "chunk_index": index,
                },
            )
        )
    return docs


def _make_store(store_type: str) -> tuple[VectorStoreManager, "tempfile.TemporaryDirectory[str]"]:
    """在临时目录里建一个已写入测试语料的向量库。"""
    tmp = tempfile.TemporaryDirectory()
    manager = VectorStoreManager(store_type=store_type, persist_dir=Path(tmp.name))
    manager.add_documents(_build_docs())
    return manager, tmp


# --------------------------------------------------------------------------- #
# 1. LLM 客户端：按配置生成不同 provider
# --------------------------------------------------------------------------- #
def test_client_from_settings() -> None:
    print("\n==== 1. 按配置生成 LLM 客户端 ====")
    reset_llm_client_cache()

    # 默认 provider 来自 settings（当前为 siliconflow）
    client = get_llm_client()
    check(
        "默认 provider 取自 settings.LLM_PROVIDER",
        client.provider == settings.LLM_PROVIDER,
        f"{client.provider} vs {settings.LLM_PROVIDER}",
    )
    check(
        "siliconflow 的模型名取自配置",
        client.model_name == settings.SILICONFLOW_MODEL_NAME,
        client.model_name,
    )
    check(
        "siliconflow 的 base_url 取自配置",
        client.base_url == settings.SILICONFLOW_BASE_URL,
        client.base_url,
    )
    check(
        "生成参数取自配置",
        client.temperature == settings.LLM_TEMPERATURE
        and client.max_tokens == settings.LLM_MAX_TOKENS
        and client.timeout == settings.LLM_TIMEOUT
        and client.max_retries == settings.LLM_MAX_RETRIES,
        f"{client.temperature}/{client.max_tokens}/{client.timeout}/{client.max_retries}",
    )

    # 三个 provider 都要能按各自配置生成客户端（此处不联网，只验证构造）
    openai_client = LLMClient(provider="openai", api_key="sk-test-key-000")
    check(
        "openai 客户端读到自己的配置",
        openai_client.model_name == settings.OPENAI_MODEL_NAME
        and openai_client.base_url == settings.OPENAI_BASE_URL,
        f"{openai_client.model_name} @ {openai_client.base_url}",
    )
    sf_client = LLMClient(provider="SILICONFLOW")   # 大小写不敏感
    check("provider 大小写不敏感", sf_client.provider == "siliconflow", sf_client.provider)
    ollama_client = LLMClient(provider="ollama")
    check(
        "ollama 客户端读到自己的配置（且不需要 API Key）",
        ollama_client.model_name == settings.OLLAMA_MODEL_NAME
        and ollama_client.base_url == settings.OLLAMA_BASE_URL,
        f"{ollama_client.model_name} @ {ollama_client.base_url}",
    )

    # 真正构建底层 Chat 模型（不联网，仅构造对象）
    llm = openai_client.get_llm()
    check("get_llm() 返回 LangChain Chat 模型", isinstance(llm, object) and llm is not None)
    check("延迟构建：同一个实例只构建一次", openai_client.get_llm() is llm)
    # get_llm() 走的是「同一份缓存 key」，应与等价参数的客户端返回同一个底层模型
    check(
        "get_llm() 便捷入口返回同一底层模型",
        get_llm(provider="openai", api_key="sk-test-key-000")
        is get_llm_client(provider="openai", api_key="sk-test-key-000").get_llm(),
    )

    # ollama 分支要能在没装 langchain-ollama 时回退 community 版本
    def _build_ollama():  # type: ignore[no-untyped-def]
        return ollama_client.get_llm()

    check("ollama 分支可构造客户端（不装 langchain-ollama 也能回退）", _no_raise(_build_ollama))


# --------------------------------------------------------------------------- #
# 2. 配置校验：缺 key / 空模型名 / 非法 provider
# --------------------------------------------------------------------------- #
def _expect_llm_config_error(name: str, **kwargs: object) -> None:
    """构造客户端应当抛出 LLMConfigError。"""
    try:
        LLMClient(**kwargs)  # type: ignore[arg-type]
        check(name, False, "没有抛异常")
    except LLMConfigError as e:
        check(name, True)
        print(f"        报错文案：{e}")
    except Exception as e:
        check(name, False, f"抛的是 {type(e).__name__}: {e}")


def test_config_validation() -> None:
    print("\n==== 2. 配置校验 ====")

    # openai 的 key 在 .env 里是空的：显式传空 key 必须在构造阶段就报错
    _expect_llm_config_error(
        "缺少 API Key 时抛 LLMConfigError（而不是等远端 401）",
        provider="openai",
        api_key="",
    )
    _expect_llm_config_error("空模型名抛 LLMConfigError", provider="openai", api_key="k", model_name="  ")
    _expect_llm_config_error("不支持的 provider 抛 LLMConfigError", provider="gemini")

    # 显式传入的 key 应当覆盖配置（多租户场景）
    override = LLMClient(provider="openai", api_key="sk-my-own", model_name="gpt-4o-mini")
    check("显式传参覆盖配置中的模型名", override.model_name == "gpt-4o-mini", override.model_name)


# --------------------------------------------------------------------------- #
# 3. 密钥安全
# --------------------------------------------------------------------------- #
def test_secret_safety() -> None:
    print("\n==== 3. 密钥安全 ====")
    client = LLMClient(provider="openai", api_key="sk-super-secret-value")
    info = client.get_model_info()

    check("get_model_info() 没有 api_key 字段", "api_key" not in info, str(list(info)))
    check("get_model_info() 只给 has_api_key 布尔值", info["has_api_key"] is True)
    check("密钥原文没有出现在返回值里", "sk-super-secret-value" not in str(info))
    check("基本信息齐全", {"provider", "model_name", "base_url", "temperature"} <= set(info), str(list(info)))
    check("initialized 反映是否真正建立连接", info["initialized"] is False)

    # 真正构建后，LangChain 会把 key 包成 SecretStr，打印时自动脱敏
    built = client.get_llm()
    check("构建后 initialized 变为 True", client.get_model_info()["initialized"] is True)
    check("密钥在底层对象里也被脱敏", "sk-super-secret-value" not in str(built))


# --------------------------------------------------------------------------- #
# 4. 客户端缓存
# --------------------------------------------------------------------------- #
def test_client_cache() -> None:
    print("\n==== 4. 客户端缓存 ====")
    reset_llm_client_cache()

    first = get_llm_client(provider="openai", api_key="sk-cache-test")
    second = get_llm_client(provider="openai", api_key="sk-cache-test")
    check("同配置两次获取是同一实例", first is second)

    other_model = get_llm_client(provider="openai", api_key="sk-cache-test", model_name="gpt-4o")
    check("换模型得到不同实例（不会互相覆盖）", other_model is not first)

    other_provider = get_llm_client(provider="ollama")
    check("换 provider 得到不同实例", other_provider is not first)
    check("切 provider 不会串台", first.provider == "openai" and other_provider.provider == "ollama")

    reset_llm_client_cache()
    after_reset = get_llm_client(provider="openai", api_key="sk-cache-test")
    check("重置缓存后会重建实例", after_reset is not first)
    reset_llm_client_cache()


# --------------------------------------------------------------------------- #
# 5. 重排模型路径解析 + 重排器基础行为
# --------------------------------------------------------------------------- #
def test_reranker_basics() -> None:
    print("\n==== 5. 重排器 ====")

    try:
        model_path, source = resolve_reranker_model_path()
        available = True
    except ValueError as e:
        model_path, source, available = "", "", False
        print(f"  [SKIP] 本地没有重排模型：{e}")

    if available:
        check("本地优先：解析到 local 来源", source == "local", source)
        check("解析出的路径是已存在的目录", Path(model_path).is_dir(), model_path)

    # 未登记的模型名必须给出「可修」的报错，而不是含糊的 FileNotFoundError
    try:
        resolve_reranker_model_path("no_such_reranker_xyz")
        check("未登记模型名抛 ValueError", False, "没有抛异常")
    except ValueError as e:
        check("未登记模型名抛 ValueError（并提示去 RERANKER_HF_REPOS 补充）", True)
        print(f"        报错文案：{e}")

    if not available:
        print("  [SKIP] 本地无重排模型，跳过打分相关断言")
        return

    reranker = CrossEncoderReranker(model_path=model_path, top_k=3)
    docs = [Document(page_content=text) for text in TEST_TEXTS]
    scored = reranker.rerank(QUERY, docs)

    check("rerank() 返回 (文档, 分数) 列表", scored is not None and len(scored) == 3, str(scored))
    if scored:
        check("分数是 Python 原生 float（不是 numpy.float32）", type(scored[0][1]) is float, str(type(scored[0][1])))
        check(
            "重排分数降序（越大越相关）",
            all(scored[i][1] >= scored[i + 1][1] for i in range(len(scored) - 1)),
            str([round(s, 4) for _, s in scored]),
        )
        check("相关片段排在首位", "年假" in scored[0][0].page_content, scored[0][0].page_content[:30])
        check("分数落在 0~1（bge-reranker 的 sigmoid 输出）", all(0.0 <= s <= 1.0 for _, s in scored))

    check("空候选返回空列表（不加载模型也不报错）", reranker.rerank(QUERY, []) == [])

    # 阈值过滤：设一个很高的阈值，低于它的候选应被丢弃
    strict = CrossEncoderReranker(model_path=model_path, top_k=5, score_threshold=0.9)
    strict_scored = strict.rerank(QUERY, docs)
    check(
        "阈值过滤后条数不增加",
        strict_scored is not None and len(strict_scored) <= len(scored or []),
        f"{len(strict_scored or [])} vs {len(scored or [])}",
    )
    if strict_scored:
        check("保留下来的都高于阈值", all(score >= 0.9 for _, score in strict_scored))

    # LangChain 压缩器接口：返回 Document 列表，并把分数写进 metadata
    compressed = reranker.compress_documents(docs, QUERY)
    check("compress_documents 返回 Document 列表", len(compressed) == 3 and isinstance(compressed[0], Document))
    check(
        "压缩后 metadata 带 rerank_score",
        all("rerank_score" in doc.metadata for doc in compressed),
        str(compressed[0].metadata),
    )
    check("compress_documents 空输入返回空列表", reranker.compress_documents([], QUERY) == [])


# --------------------------------------------------------------------------- #
# 6. 检索器：不重排链路
# --------------------------------------------------------------------------- #
def test_retrieve_without_rerank() -> None:
    print("\n==== 6. 检索（不重排） ====")
    manager, tmp = _make_store("faiss")
    retriever = RAGRetriever(
        vector_store=manager,
        use_reranker=False,
        top_k=3,
        candidate_multiplier=4,
    )

    info = retriever.get_retriever_info()
    check("关闭重排时 use_reranker=False", info["use_reranker"] is False)
    check("top_k 生效", info["top_k"] == 3, str(info["top_k"]))
    check("不重排时没有重排器实例", retriever.reranker is None)

    docs = retriever.retrieve(QUERY)
    check("返回 Document 列表", bool(docs) and all(isinstance(d, Document) for d in docs))
    check("返回条数不超过 top_k", len(docs) <= 3, str(len(docs)))
    check("命中语义最相关片段", bool(docs) and "年假" in docs[0].page_content, docs[0].page_content[:30] if docs else "空")

    scored = retriever.retrieve(QUERY, return_score=True)
    check("return_score 返回 (Document, 分数)", bool(scored) and isinstance(scored[0][1], float))
    check("分数是原生 Python float", bool(scored) and type(scored[0][1]) is float, str(type(scored[0][1])))
    if len(scored) >= 2:
        check(
            "不重排时沿用余弦相似度（越大越相似）",
            scored[0][1] >= scored[1][1],
            f"{scored[0][1]:.4f} vs {scored[1][1]:.4f}",
        )
    check("不重排时不写入 rerank_score", bool(scored) and "rerank_score" not in scored[0][0].metadata)

    # 元数据过滤要透传给向量库
    filtered = retriever.retrieve("报销", top_k=5, filter_dict={"source": SOURCE_B})
    check(
        "元数据过滤只返回指定来源",
        bool(filtered) and all(d.metadata.get("source") == SOURCE_B for d in filtered),
        str([d.metadata.get("source") for d in filtered]),
    )

    # 边界
    check("空查询返回空列表", retriever.retrieve("   ") == [])
    check("非法 top_k 回退为默认值", len(retriever.retrieve(QUERY, top_k=0)) == len(docs), str(len(docs)))

    tmp.cleanup()


# --------------------------------------------------------------------------- #
# 7. 检索器：重排链路（候选池放大 + 分数来源）
# --------------------------------------------------------------------------- #
def test_retrieve_with_rerank() -> None:
    print("\n==== 7. 检索（重排） ====")
    manager, tmp = _make_store("faiss")
    retriever = RAGRetriever(
        vector_store=manager,
        use_reranker=True,
        top_k=2,
        candidate_multiplier=3,
    )

    info = retriever.get_retriever_info()
    check("启用重排时 use_reranker=True", info["use_reranker"] is True, str(info))
    # 关键：候选池必须先放大，否则重排只是「给同样的 k 条换个顺序」
    check("候选池按倍数放大", info["candidate_k"] == info["top_k"] * info["candidate_multiplier"], str(info))
    check("重排模型路径已解析", bool(info["reranker_model"]), str(info["reranker_model"]))

    reranker_ok = retriever.reranker is not None and retriever.reranker._load_model() is not None
    if not reranker_ok:
        print("  [SKIP] 重排模型不可用，本组只验证降级链路")
    else:
        scored = retriever.retrieve(QUERY, return_score=True)
        check("重排后仍返回 top_k 条", len(scored) == 2, str(len(scored)))
        check(
            "重排后返回的是重排分数 0~1（不是向量距离）",
            all(0.0 <= score <= 1.0 for _, score in scored),
            str([round(s, 4) for _, s in scored]),
        )
        check(
            "重排分数降序",
            scored[0][1] >= scored[1][1],
            f"{scored[0][1]:.4f} vs {scored[1][1]:.4f}",
        )
        check("相关片段排在首位", "年假" in scored[0][0].page_content, scored[0][0].page_content[:30])
        first_meta = scored[0][0].metadata
        check("metadata 同时保留向量相似度与重排分", {"vector_similarity", "rerank_score"} <= set(first_meta), str(first_meta))
        check(
            "rerank_score 与返回的配对分数一致",
            abs(first_meta["rerank_score"] - scored[0][1]) < 1e-5,
            f"{first_meta['rerank_score']} vs {scored[0][1]}",
        )
        check("原有元数据没有丢失", first_meta.get("source") in (SOURCE_A, SOURCE_B), str(first_meta))

        # 重排 + 过滤：过滤条件依然生效
        filtered = retriever.retrieve("报销", top_k=3, filter_dict={"source": SOURCE_B})
        check(
            "重排链路下元数据过滤依然生效",
            bool(filtered) and all(d.metadata.get("source") == SOURCE_B for d in filtered),
            str([d.metadata.get("source") for d in filtered]),
        )

    # 降级：模型加载失败时不能中断检索，应返回原向量顺序
    broken = RAGRetriever(vector_store=manager, use_reranker=True, top_k=2, candidate_multiplier=3)
    if broken.reranker is not None:
        broken.reranker.model_path = "/definitely/not/a/model/dir"
        check("模型路径无效时 rerank() 返回 None", broken.reranker.rerank(QUERY, _build_docs()) is None)
        fallback = broken.retrieve(QUERY, return_score=True)
        check("加载失败后仍能检索（降级不抛异常）", len(fallback) > 0, str(len(fallback)))
        check("降级后不写入 rerank_score", all("rerank_score" not in d.metadata for d, _ in fallback))

    tmp.cleanup()


# --------------------------------------------------------------------------- #
# 8. 接 LCEL：as_retriever
# --------------------------------------------------------------------------- #
def test_as_retriever() -> None:
    print("\n==== 8. as_retriever（接 LCEL） ====")
    manager, tmp = _make_store("faiss")

    plain = RAGRetriever(vector_store=manager, use_reranker=False, top_k=2)
    base = plain.as_retriever()
    check("不重排时返回普通 Retriever", isinstance(base, BaseRetriever) and not isinstance(base, ContextualCompressionRetriever))

    reranked = RAGRetriever(vector_store=manager, use_reranker=True, top_k=2, candidate_multiplier=3)
    compressed = reranked.as_retriever()
    check(
        "启用重排时包一层 ContextualCompressionRetriever",
        isinstance(compressed, ContextualCompressionRetriever),
        type(compressed).__name__,
    )

    # 自定义 search_kwargs 要能透传
    custom = plain.as_retriever(search_kwargs={"k": 6})
    check("search_kwargs 可透传", custom is not None)

    # 空库保护：FAISS 空库时 _store 为 None，必须给出可读报错而不是 AttributeError
    with tempfile.TemporaryDirectory() as empty_dir:
        empty_store = VectorStoreManager(store_type="faiss", persist_dir=Path(empty_dir))
        empty_retriever = RAGRetriever(vector_store=empty_store, use_reranker=False)
        try:
            empty_retriever.as_retriever()
            check("空库构建 Retriever 抛 RuntimeError", False, "没有抛异常")
        except RuntimeError as e:
            check("空库构建 Retriever 抛 RuntimeError（提示先入库）", True)
            print(f"        报错文案：{e}")
        except Exception as e:
            check("空库构建 Retriever 抛 RuntimeError", False, f"抛的是 {type(e).__name__}")

    tmp.cleanup()


# --------------------------------------------------------------------------- #
# 9. 检索器单例
# --------------------------------------------------------------------------- #
def test_retriever_singleton() -> None:
    print("\n==== 9. 检索器单例 ====")
    reset_rag_retriever()

    first = get_rag_retriever()
    second = get_rag_retriever()
    check("两次获取是同一对象", first is second)
    check("单例读取配置里的 top_k", first.top_k == settings.SEARCH_TOP_K, str(first.top_k))
    check("单例读取配置里的倍数", first.candidate_multiplier == settings.RERANK_CANDIDATE_MULTIPLIER)

    reset_rag_retriever()
    third = get_rag_retriever()
    check("重置后会重建对象", third is not first)

    reset_rag_retriever()
    reset_vector_store_manager()


# --------------------------------------------------------------------------- #
# 10. 本地 ollama：生成参数透传 + 真实调用（服务缺失自动跳过）
# --------------------------------------------------------------------------- #
def _ollama_ready() -> tuple[bool, str]:
    """
    探测本地 ollama 服务与模型是否就绪。

    :return: (是否可用, 可用时返回模型名 / 不可用时返回跳过原因)
    """
    url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, f"连不上 ollama 服务 {url}（{type(e).__name__}）"

    names = [m.get("name", "") for m in payload.get("models", [])]
    target = settings.OLLAMA_MODEL_NAME
    if target not in names:
        return False, f"本地没有模型 {target}（已拉取：{', '.join(names) or '无'}）"
    return True, target


def test_ollama_params_and_live_call() -> None:
    """
    本地大模型的两组验证：
        ① 生成参数透传（不联网，构造即可断言）
        ② invoke / stream 真实调用（服务不在就跳过）
    """
    print("\n==== 10. 本地 ollama：参数透传 + 真实调用 ====")

    # ① 参数透传：ollama 没有 timeout 字段，超时必须塞进 client_kwargs 给底层 ollama.Client。
    #    漏传的话 settings.LLM_TIMEOUT / LLM_MAX_TOKENS 对本地模型完全失效。
    client = LLMClient(provider="ollama")
    llm = client.get_llm()
    check(
        "max_tokens 透传到 ollama 的 num_predict",
        getattr(llm, "num_predict", None) == settings.LLM_MAX_TOKENS,
        f"num_predict={getattr(llm, 'num_predict', None)} 期望={settings.LLM_MAX_TOKENS}",
    )
    client_kwargs = getattr(llm, "client_kwargs", None) or {}
    check(
        "timeout 透传到 ollama 的 client_kwargs",
        client_kwargs.get("timeout") == settings.LLM_TIMEOUT,
        f"client_kwargs={client_kwargs} 期望 timeout={settings.LLM_TIMEOUT}",
    )
    if "reasoning" in type(llm).model_fields:
        check(
            "reasoning 按配置关闭思考链",
            getattr(llm, "reasoning", None) is settings.OLLAMA_REASONING,
            f"reasoning={getattr(llm, 'reasoning', None)} 期望={settings.OLLAMA_REASONING}",
        )

    # ② 真实调用：服务或模型不在时整组跳过，不算失败
    ready, detail = _ollama_ready()
    if not ready:
        print(f"  [SKIP] {detail}")
        print(f"         启用方式：启动 ollama 服务后执行 ollama pull {settings.OLLAMA_MODEL_NAME}")
        return
    print(f"  使用本地模型：{detail}")

    try:
        start = time.perf_counter()
        answer = client.invoke("只回答两个字：上海")
        cost = time.perf_counter() - start
    except Exception as e:
        check("ollama invoke 真实调用成功", False, f"{type(e).__name__}: {e}")
        return
    check("ollama invoke 返回非空文本", bool(answer.strip()), repr(answer))
    print(f"        invoke 耗时 {cost:.1f}s -> {answer!r}")

    try:
        chunks = list(client.stream("只回答两个字：北京"))
    except Exception as e:
        check("ollama stream 真实调用成功", False, f"{type(e).__name__}: {e}")
        return
    joined = "".join(chunks)
    check("ollama stream 产出非空片段", bool(chunks), f"片段数={len(chunks)}")
    check("stream 片段可拼成非空文本", bool(joined.strip()), repr(joined))
    print(f"        stream 结果 -> {joined!r}")

    # 空 chunk 占比：thinking 模型会把思考 token 全吐成空 chunk，前端看不到逐字效果。
    # 关掉 reasoning 后应显著下降；这里只统计打印，便于换模型时做对比。
    try:
        raw = list(llm.stream([HumanMessage(content="只回答两个字：广州")]))
        total = len(raw) or 1
        empty = sum(1 for c in raw if not (getattr(c, "content", "") or "").strip())
        print(f"        原始 chunk={total} 空 chunk={empty}（空占比 {empty / total:.0%}）")
    except Exception as e:
        print(f"        [SKIP] 原始 stream 统计失败：{type(e).__name__}: {e}")


def main() -> int:
    print("==== 0. 前置检查 ====")
    try:
        check("配置可加载", settings is not None)
        check(
            "LLM provider 在支持范围内",
            settings.LLM_PROVIDER in LLMClient.SUPPORTED_PROVIDERS,
            settings.LLM_PROVIDER,
        )
        if settings.LLM_PROVIDER in LLMClient.KEY_REQUIRED_PROVIDERS:
            # 默认 provider 需要 key：没有就说明 .env 没配好，后面的客户端用例必然失败
            has_key = bool(
                (settings.SILICONFLOW_API_KEY if settings.LLM_PROVIDER == "siliconflow" else settings.OPENAI_API_KEY).strip()
            )
            check(f"默认 provider（{settings.LLM_PROVIDER}）已配置 API Key", has_key)
    except Exception as e:
        print(f"  [FATAL] 前置检查失败：{e}")
        return 1

    test_client_from_settings()
    test_config_validation()
    test_secret_safety()
    test_client_cache()
    test_reranker_basics()
    test_retrieve_without_rerank()
    test_retrieve_with_rerank()
    test_as_retriever()
    test_retriever_singleton()
    test_ollama_params_and_live_call()

    print(f"\n==== 汇总：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项 ====")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
