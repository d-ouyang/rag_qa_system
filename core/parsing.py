"""
文档解析的唯一入口 —— 「读盘 → 解析 → 切分 → 写向量库」这段逻辑在整个项目里只此一份。

--------------------------------------------------------------------------
为什么必须收敛成一份
--------------------------------------------------------------------------
P0-3 之前，这段逻辑长在 `api/routes/documents.py` 的 upload 路由里（同步执行）。
P0-3 把它挪到 worker，P0-4 还要写一个 `scripts/reindex.py` 扫描 `upload/` 重灌。

如果没有这个模块，就会自然长出三份「差不多」的实现：
    路由里一份（历史遗留）、worker 里一份、reindex 里一份
三份的差异会体现在最要命的地方：**metadata 字段名**。
重建脚本漏写 `doc_id`，于是重建之后「按文档删除」静默失效；
少了 `chunk_index`，于是片段顺序变成随机 —— 而这两种错都不会报错，
只会让人在几周后对着检索结果困惑「为什么删了文档还能被召回」。

所以这里定死：任何需要「把文件变成向量库切片」的地方，都调 `parse_and_index()`。

--------------------------------------------------------------------------
关于 metadata 的字段约定（P0-3 写、P0-4 用）
--------------------------------------------------------------------------
    doc_id      int     document 表主键。**删除与反查的身份键**（不是路径）
    project_id  str     归属项目，多租户过滤用
    chunk_index int     该片段在文档内的序号，从 0 开始，用于稳定排序
    chunk_id    str     切片引用键 `"<doc_id>:<chunk_index>"`（P0-4a 新增）。
                        `sources` 带着它回前端，用户点引用 → `GET /api/v1/chunks/{chunk_id}`
                        → 反查正文；同时它也是 chat_message.ref_ids 里存的值。
                        这里写一份是让 Chroma 里那条记录**自描述**（外部工具/人
                        直接看库也认得出引用键）；读取侧仍会从 doc_id+chunk_index
                        兜底重拼一次，所以这个字段缺失不会让老切片变成死链路。
    source      str     磁盘**绝对**路径（沿用旧语义，兼容 delete_by_source 与老数据）
    file_name   str     **原始**文件名（不是 uuid 落盘名），展示用
    file_type   str     无点后缀
    page        int?    PDF/PPT 的页码，由 Loader 自己带（可能没有）

`doc_id`、`chunk_index`、`chunk_id` 是新增的。前两个必须在**写入时**就带上 ——
补不回来（事后无法从切片内容反推它属于哪次上传），所以这一步做漏了
就得整库重建一次。这也是把 P0-4 的一部分提前到 P0-3 的唯一理由。
`chunk_id` 是前两者的派生值，缺了还能补；但补出来的前提是前两个字段在 ——
这也是「先有 doc_id/chunk_index、再有 chunk_id」这个顺序不能颠倒的原因。

--------------------------------------------------------------------------
关于 doc_id 的类型
--------------------------------------------------------------------------
Chroma 的元数据支持 int，而 `where={"doc_id": 11}` 只在类型完全一致时命中
（int 11 与 str "11" 是两条不同的检索条件）。这里统一写 **int**，
与 MySQL 的 BIGINT 对齐；`VectorStoreManager._normalize_doc_id()` 在读取侧
做同样的归一，两边靠同一份约定而不是靠运气对上。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config.settings import settings
from core import document_repo as repo
from core.document_loader import DocumentLoader
from core.vector_store import VectorStoreManager, build_chunk_id, get_vector_store_manager

logger = logging.getLogger(__name__)

# 写进 MySQL `fail_reason` 的文案长度上限。列宽 512，留出余量给异常类型前缀。
# 之所以在这里也截一次（仓储层已经截过）：这一层知道「哪些部分是废话」，
# 能把最有用的一句留在前面，而不是让 SQL 异常的长尾巴把真实原因挤掉。
_MAX_REASON_LEN = 480


class ParseError(RuntimeError):
    """
    解析失败。**message 会被原样写进 MySQL `fail_reason` 并展示给用户**，
    所以每条 message 都要满足两个条件：短，且能指出下一步该做什么。

    反例（不要这么写）：`KeyError: 'pages'`。用户看不懂，也不知道该改什么。
    正例：`不支持的文件类型 '.bin'`、`PDF 已加密，无法提取文本`。
    """


# 已知的「基础设施类异常」→ 人话。键是异常类名而不是类型对象。
#
# 为什么按**类名**匹配而不是 `except SoftTimeLimitExceeded`：
# 那个异常来自 Celery，而 core 层不该 import 队列框架 ——
# 一旦 import，`core.parsing` 就再也无法在没装 Celery 的环境里被使用，
# 而「只想重建向量库」的运维脚本完全不需要队列。
# 用类名做一次字符串判断，是这里最小代价的解耦。
#
# 背景：Celery 的软超时（task_soft_time_limit）在任务里抛
# SoftTimeLimitExceeded，它会先被上面那个 `except Exception` 接住。
# 若不特判，写进 fail_reason 的会是 "SoftTimeLimitExceeded: "（消息为空），
# 用户看到一行英文类名，完全不知道发生了什么、该调哪个参数。
_INFRA_FAILURE_HINTS: dict[str, str] = {
    "SoftTimeLimitExceeded": "解析超时被中止（文件可能过大或结构异常），可调整 TASK_SOFT_TIME_LIMIT_SECONDS 后重试",
    "TimeLimitExceeded": "解析超过硬超时被强制终止（文件可能过大或结构异常）",
    "OperationalError": "数据库暂时不可用，请稍后重试",
    "MemoryError": "解析过程中内存不足（文件可能过大）",
}


def _describe_unexpected(exc: BaseException) -> str:
    """
    把未预期异常转成一句可写进 `fail_reason` 的话。

    规则：已知的基础设施异常查表得人话；其余保留 `类名: 消息` ——
    未预期异常的价值恰恰在于它的原始类名，翻译掉反而挡住排查线索。
    """
    name = type(exc).__name__
    hint = _INFRA_FAILURE_HINTS.get(name)
    if hint:
        return hint
    message = str(exc).strip()
    return f"{name}: {message}" if message else name


# --------------------------------------------------------------------------- #
# 第一层：纯解析，不碰 MySQL
# --------------------------------------------------------------------------- #
def resolve_storage_path(storage_path: str) -> Path:
    """
    把 MySQL 里存的 `storage_path` 还原成真实路径。

    约定：库里存的是**相对于项目根**的路径（如 `upload/3f2a....pdf`），
    不是绝对路径。理由见 `alembic/versions/0001` 里该列的注释 ——
    绝对路径一旦进了数据库，本地开发（`/Users/xxx/...`）和容器
    （`/app/upload`）就会互相读不到对方的文件，且没有配置项能纠正它。

    兼容一次绝对路径：早期手工入库的行可能是绝对的，此时直接用它，
    不做「相对根」的拼接（否则会得到 `/项目根//Users/...` 这种鬼路径）。
    """
    path = Path(storage_path)
    return path if path.is_absolute() else (settings.BASE_DIR / path)


def parse_and_index(
    doc_id: int,
    storage_path: str,
    *,
    file_name: str | None = None,
    project_id: str = "default",
    loader: DocumentLoader | None = None,
    store: VectorStoreManager | None = None,
) -> int:
    """
    把一个文件解析、切分并写进向量库。**返回实际写入的切片数**。

    失败一律抛 `ParseError`（带可读原因）—— 由调用方决定怎么记录状态。
    本函数**不写 MySQL**：它不知道自己是不是某个任务的一部分，
    把状态流转塞进来会让「只想重建向量库」的脚本也得先有一个 document 行。

    幂等性：写入前先 `delete_by_doc_id`，所以同一 doc_id 跑多少次
    都不会出现重复切片（这是 P0-3 验收标准第 4 条）。

    顺序为什么是「先解析、再删、再写」而不是「先删、再解析、再写」：
    「删」与「写」必须紧挨着，因为删完到写完之间是**数据不可用窗口**。
    若先删再解析，一个大 PDF 会带来几分钟的空窗（这段时间检索不到它，
    引用反查也拿不到正文）；放在解析之后，窗口只有毫秒级。
    额外好处：解析失败时旧切片原封不动 —— 而旧切片的内容与本次尝试的
    是同一个文件，保留它严格优于把它删掉。
    """
    path = resolve_storage_path(storage_path)
    display_name = file_name or path.name

    # ---- 1. 前置校验：这些错误能用一句话说清，没必要丢给 Loader 去猜 ----
    if not path.exists():
        raise ParseError(f"文件不存在：{display_name}")
    if not path.is_file():
        raise ParseError(f"路径不是文件：{display_name}")
    size = path.stat().st_size
    if size == 0:
        raise ParseError(f"文件内容为空：{display_name}")

    loader = loader or DocumentLoader()
    store = store or get_vector_store_manager()

    ext = path.suffix.lower()
    if ext not in loader.SUPPORTED_EXTENSIONS:
        raise ParseError(
            f"不支持的文件类型 '{ext}'，当前支持："
            f"{'/'.join(sorted(e.lstrip('.') for e in loader.SUPPORTED_EXTENSIONS))}"
        )

    # ---- 2. 解析 + 切分 ----
    # metadata 经 load_file 合并后会跟着 split_documents 传递到每个切片。
    # 传 file_name 是为了**覆盖** Loader 内置的那个 —— 它取的是 path.name，
    # 而 P0-3 之后 path 是 uuid 名，直接用会把 uuid 展示给用户。
    documents = loader.load_file(
        path,
        metadata={"doc_id": int(doc_id), "project_id": project_id, "file_name": display_name},
    )
    if not documents:
        # load_file 内部吞掉了真实异常并返回空列表（这是它在「目录批量导入」场景
        # 下的正确行为：一个坏文件不该中断整批）。但在这里「空结果」只有一个含义，
        # 所以把话说明白：要么损坏，要么没有可提取文本。
        raise ParseError(f"解析失败或没有可提取文本（文件可能已损坏）：{display_name}")

    # ---- 3. 补上切片序号与引用键 ----
    # 必须在切分**之后**编号：切分前的编号是「原始段」的序号，与最终切片不是一一对应。
    # 显式写 int，防止某些 Loader 把元数据值转成字符串。
    # chunk_id 顺手一起写：它是 (doc_id, chunk_index) 的纯派生值，
    # 放在同一个循环里能保证两者永远同源 —— 分开写就多了一个「只改了一处」的机会。
    for index, doc in enumerate(documents):
        doc.metadata["doc_id"] = int(doc_id)
        doc.metadata["chunk_index"] = index
        doc.metadata["chunk_id"] = build_chunk_id(doc_id, index)

    # ---- 4. 幂等写入：先清旧切片，再写新的 ----
    removed = store.delete_by_doc_id(doc_id)
    if removed:
        logger.info("已清理上一次的残留切片 | doc_id=%s | 数量=%d", doc_id, removed)

    added = store.add_documents(documents)
    if added == 0:
        raise ParseError("向量库写入失败（嵌入或落库环节出错，详见后端日志）")

    logger.info(
        "解析入库完成 | doc_id=%s | 文件=%s | 切片=%d | 清理旧切片=%d",
        doc_id, display_name, added, removed,
    )
    return added


# --------------------------------------------------------------------------- #
# 第二层：完整任务（状态机），Celery 只是它的外壳
# --------------------------------------------------------------------------- #
def run_parse_task(
    doc_id: int,
    *,
    stale_seconds: int | None = None,
    store: VectorStoreManager | None = None,
) -> dict[str, Any]:
    """
    跑完一个解析任务：**抢任务 → 解析入库 → 写回状态**。永不抛异常。

    为什么本函数与 Celery task 分开：Celery task 是一个进程边界上的包装，
    要跑起来得有 broker、有 worker、有 Redis。而这条状态机才是真正需要被
    反复验证的逻辑（抢不到怎么办、解析炸了怎么写状态、文件被删了怎么办）。
    分开之后，测试可以直接调本函数覆盖整条链路**不需要任何队列基础设施**。

    永不抛异常的理由：抛出会让 Celery 认为任务失败并（在配置了重试时）
    反复重试。而「文件损坏」这种失败重试 N 次的结果完全一样，
    只是把 CPU 烧 N 遍。失败一律落到 `document.status = fail`，
    要不要再试由人决定（`POST /reparse`）。

    唯一的例外是**数据库本身不可用**：那时 `mark_fail` 也会失败，
    异常会穿透出去 —— 这是对的。记录留在 parsing，等孤儿超时后
    可以被重新抢走（见迁移 0002），比静默吞掉一个「状态没写成功」要好。

    :return: 结构化结果，便于 Celery 日志与测试断言
    """
    # ---- 抢任务 ----
    if not repo.try_claim(doc_id, stale_seconds=stale_seconds):
        # 常见原因：重复入队（同一 doc_id 被推了两次）、已被兄弟 worker 拿走、
        # 或者这条记录当前是 success。三种都不该干活。
        return {"doc_id": doc_id, "claimed": False, "ok": False, "reason": "未抢到任务（已被处理或状态不允许）"}

    record = repo.get(doc_id)
    if record is None:
        # 抢到之后、读出来之前被删了。不加这一层判断会直接
        # AttributeError: 'NoneType'，日志里看不出发生了什么。
        logger.warning("抢到任务但记录已被删除 | doc_id=%s", doc_id)
        return {"doc_id": doc_id, "claimed": True, "ok": False, "reason": "记录已被删除"}

    # ---- 解析 ----
    try:
        chunk_count = parse_and_index(
            record.doc_id,
            record.storage_path,
            file_name=record.file_name,
            project_id=record.project_id,
            store=store,
        )
    except ParseError as e:
        # 预期内的失败（文件坏 / 类型不支持 / 内容为空）：message 是给人看的
        repo.mark_fail(doc_id, str(e))
        return {"doc_id": doc_id, "claimed": True, "ok": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001 - 兜底：任何未预期异常都要变成可读的 fail_reason
        detail = _describe_unexpected(e)
        logger.error("解析任务出现未预期异常 | doc_id=%s | %s", doc_id, detail, exc_info=True)
        repo.mark_fail(doc_id, detail[:_MAX_REASON_LEN])
        return {"doc_id": doc_id, "claimed": True, "ok": False, "reason": detail}

    # ---- 写回状态 ----
    repo.mark_success(doc_id, chunk_count)
    return {"doc_id": doc_id, "claimed": True, "ok": True, "chunk_count": chunk_count}
