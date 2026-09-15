# pyright: basic
"""
模块2测试文件：验证文档加载与切分逻辑（core/document_loader.py）

运行：
    .venv/bin/python tests/test_module2_document_loader.py      （或 make test）

样本文件：tests/sample_docs/（由 tests/sample_docs/generate_samples.py 生成，内容源为 test.txt）

覆盖点：
1. 后缀白名单与边界校验（不存在 / 不支持后缀 / 无后缀 / 目录当文件 / 文件当目录 / 空文件）
2. 九种格式的真实样本加载（txt md docx xlsx pptx pdf csv json html）
3. 元数据注入（文档类型 file_type / 文件名 file_name / 文件路径 source）
4. 切分参数（chunk_size）生效
5. 目录批量加载（递归 / 非递归 / 跳过隐藏项与不支持类型 / 单文件失败不中断）
6. 编码回退（GBK 文本与 CSV）
7. 附加元数据与同名字段覆盖优先级
8. 同名文件检测（文件名相同、来源不同时给出告警，且两份都保留）

备注：.md 走 unstructured 结构化解析时需要 NLTK 的 punkt 数据，缺失时会自动回退为
纯文本读取（功能不受影响，但每条日志会提示）。如需启用结构化解析，执行一次：
    .venv/bin/python -m nltk.downloader punkt_tab
"""
import logging
import sys
import tempfile
from pathlib import Path

# 想要导入自定义包或者模块，建议将项目根目录加入系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.documents import Document

from config.logging_config import setup_logging
from config.settings import settings
from core.document_loader import DocumentLoader

# 初始化日志
setup_logging()

SAMPLE_DIR: Path = Path(__file__).parent / "sample_docs"

# 样本文件 → (文档类型, 正文中必现的关键词)
SAMPLE_CASES: dict[str, tuple[str, str]] = {
    "test.txt": ("txt", "考勤"),
    "test.md": ("md", "考勤"),
    "test.docx": ("docx", "考勤"),
    "test.xlsx": ("xlsx", "事假"),
    "test.pptx": ("pptx", "考勤"),
    "test.pdf": ("pdf", "考勤"),
    "test.csv": ("csv", "事假"),
    "test.json": ("json", "考勤"),
    "test.html": ("html", "考勤"),
}

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


class WarningRecorder(logging.Handler):
    """临时挂到 document_loader 的 logger 上，用于断言告警确实发出（验证「让人知道」）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# --------------------------------------------------------------------------- #
# 1. 后缀白名单
# --------------------------------------------------------------------------- #
def test_supported_extensions() -> None:
    print("\n==== 1. 后缀白名单 ====")
    loader = DocumentLoader()
    for ext in ["pdf", "doc", "docx", "txt", "md", "xlsx", "xls", "pptx", "csv", "json", "html", "htm"]:
        check(f"支持 .{ext}", loader.is_supported(f"sample.{ext}"))
    check("大写后缀也识别", loader.is_supported("sample.PDF"))
    check("拒绝 .bin", not loader.is_supported("sample.bin"))
    check("拒绝无后缀", not loader.is_supported("README"))
    check("白名单共 12 种", len(loader.SUPPORTED_EXTENSIONS) == 12, str(len(loader.SUPPORTED_EXTENSIONS)))


# --------------------------------------------------------------------------- #
# 2. 各格式样本加载 + 元数据
# --------------------------------------------------------------------------- #
def test_load_single_files() -> None:
    print("\n==== 2. 各格式样本加载与元数据 ====")
    loader = DocumentLoader()
    for file_name, (file_type, keyword) in SAMPLE_CASES.items():
        path = SAMPLE_DIR / file_name
        if not path.exists():
            check(f"{file_name} 样本存在", False, "请先运行 tests/sample_docs/generate_samples.py")
            continue

        docs = loader.load_file(path)
        check(f"{file_name} 解析出片段", len(docs) > 0, f"片段数={len(docs)}")
        if not docs:
            continue

        check(f"{file_name} 返回 Document 对象", all(isinstance(d, Document) for d in docs))
        check(f"{file_name} 文件类型 = {file_type}", docs[0].metadata.get("file_type") == file_type)
        check(f"{file_name} 文件名正确", docs[0].metadata.get("file_name") == file_name)
        check(
            f"{file_name} 文件路径正确",
            Path(str(docs[0].metadata.get("source", ""))).name == file_name,
            str(docs[0].metadata.get("source")),
        )
        meta_complete = all(
            isinstance(d.metadata.get("source"), str)
            and isinstance(d.metadata.get("file_name"), str)
            and isinstance(d.metadata.get("file_type"), str)
            for d in docs
        )
        check(f"{file_name} 每个片段元数据齐全", meta_complete)
        check(
            f"{file_name} 正文命中关键词「{keyword}」",
            any(keyword in d.page_content for d in docs),
        )


# --------------------------------------------------------------------------- #
# 3. 切分参数
# --------------------------------------------------------------------------- #
def test_chunk_params() -> None:
    print("\n==== 3. 切分参数 ====")
    path = SAMPLE_DIR / "test.txt"
    small = DocumentLoader(chunk_size=100, chunk_overlap=10)
    large = DocumentLoader(chunk_size=1000, chunk_overlap=100)
    docs_small = small.load_file(path)
    docs_large = large.load_file(path)

    check("chunk_size 越小、片段越多", len(docs_small) > len(docs_large), f"{len(docs_small)} vs {len(docs_large)}")
    max_len = max((len(d.page_content) for d in docs_small), default=0)
    check("片段长度不超过 chunk_size", max_len <= 100, f"实际最大 {max_len}")
    check("默认参数读取 settings.CHUNK_SIZE", DocumentLoader().chunk_size == settings.CHUNK_SIZE)
    check("默认参数读取 settings.CHUNK_OVERLAP", DocumentLoader().chunk_overlap == settings.CHUNK_OVERLAP)


# --------------------------------------------------------------------------- #
# 4. 目录批量加载
# --------------------------------------------------------------------------- #
def test_load_directory() -> None:
    print("\n==== 4. 目录批量加载 ====")
    loader = DocumentLoader()
    recursive = loader.load_directory(SAMPLE_DIR)
    flat = loader.load_directory(SAMPLE_DIR, recursive=False)
    recursive_files = {d.metadata["file_name"] for d in recursive}
    flat_files = {d.metadata["file_name"] for d in flat}

    check("递归结果非空", len(recursive) > 0)
    check("递归覆盖顶层全部样本", set(SAMPLE_CASES) <= recursive_files, str(sorted(recursive_files)))
    check("递归包含子目录文件 extra.md", "extra.md" in recursive_files)
    check("非递归不含子目录文件", "extra.md" not in flat_files)
    check("非递归 = 顶层样本集合", flat_files == set(SAMPLE_CASES), str(sorted(flat_files)))
    check("递归片段数 > 非递归片段数", len(recursive) > len(flat), f"{len(recursive)} vs {len(flat)}")


# --------------------------------------------------------------------------- #
# 5. 边界校验
# --------------------------------------------------------------------------- #
def test_edge_cases() -> None:
    print("\n==== 5. 边界校验 ====")
    loader = DocumentLoader()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        sub_dir = tmp_dir / "sub"
        sub_dir.mkdir()

        check("不存在的文件返回空", loader.load_file(tmp_dir / "nope.txt") == [])
        check("不存在的目录返回空", loader.load_directory(tmp_dir / "nope_dir") == [])

        bin_file = tmp_dir / "data.bin"
        bin_file.write_text("not supported", encoding="utf-8")
        check("不支持的后缀返回空", loader.load_file(bin_file) == [])

        no_ext = tmp_dir / "README"
        no_ext.write_text("no ext", encoding="utf-8")
        check("无后缀文件返回空", loader.load_file(no_ext) == [])

        check("目录传给 load_file 返回空", loader.load_file(sub_dir) == [])
        check("文件传给 load_directory 返回空", loader.load_directory(bin_file) == [])

        empty_txt = tmp_dir / "empty.txt"
        empty_txt.write_text("", encoding="utf-8")
        check("空文本文件返回空", loader.load_file(empty_txt) == [])

        # 单文件损坏不应中断整目录加载
        (tmp_dir / "ok.txt").write_text("正常内容。" * 30, encoding="utf-8")
        (tmp_dir / "broken.docx").write_text("这不是合法的 docx 内容", encoding="utf-8")
        docs = loader.load_directory(tmp_dir)
        names = {d.metadata["file_name"] for d in docs}
        check("坏文件被跳过、好文件仍加载", "ok.txt" in names, str(sorted(names)))
        check("坏文件未产生片段", "broken.docx" not in names, str(sorted(names)))


# --------------------------------------------------------------------------- #
# 6. 目录跳过规则
# --------------------------------------------------------------------------- #
def test_directory_skip_rules() -> None:
    print("\n==== 6. 目录跳过规则 ====")
    loader = DocumentLoader()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "normal.txt").write_text("正常文档内容。" * 30, encoding="utf-8")
        (tmp_dir / ".hidden.txt").write_text("隐藏文件内容。" * 30, encoding="utf-8")
        (tmp_dir / "~$draft.txt").write_text("临时文件内容。" * 30, encoding="utf-8")
        (tmp_dir / "skip.bin").write_text("不支持的类型", encoding="utf-8")
        hidden_dir = tmp_dir / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "inside.txt").write_text("隐藏目录内的文档。" * 30, encoding="utf-8")

        docs = loader.load_directory(tmp_dir)
        names = {d.metadata["file_name"] for d in docs}
        check("只加载正常文件", names == {"normal.txt"}, str(sorted(names)))


# --------------------------------------------------------------------------- #
# 7. 编码回退
# --------------------------------------------------------------------------- #
def test_encoding_fallback() -> None:
    print("\n==== 7. 编码回退 ====")
    loader = DocumentLoader()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        gbk_txt = tmp_dir / "gbk.txt"
        gbk_txt.write_bytes("企业内部考勤管理制度。".encode("gbk"))
        txt_docs = loader.load_file(gbk_txt)
        check("GBK 文本可加载", len(txt_docs) > 0)
        check("GBK 文本内容正确", bool(txt_docs) and "考勤" in txt_docs[0].page_content)

        gbk_csv = tmp_dir / "gbk.csv"
        gbk_csv.write_bytes("问题,答案\n考勤,管理制度\n".encode("gbk"))
        csv_docs = loader.load_file(gbk_csv)
        check("GBK CSV 可加载", len(csv_docs) > 0)
        check("GBK CSV 内容正确", bool(csv_docs) and "考勤" in csv_docs[0].page_content)


# --------------------------------------------------------------------------- #
# 8. 附加元数据与覆盖优先级
# --------------------------------------------------------------------------- #
def test_metadata_override() -> None:
    print("\n==== 8. 附加元数据 ====")
    loader = DocumentLoader()
    docs = loader.load_file(SAMPLE_DIR / "test.txt", metadata={"file_name": "原始名.txt", "category": "hr"})
    check("返回片段非空", len(docs) > 0)
    if not docs:
        return
    check("自定义字段写入成功", docs[0].metadata.get("category") == "hr")
    check("同名字段以调用方为准", docs[0].metadata.get("file_name") == "原始名.txt")
    check("内置字段仍然保留", docs[0].metadata.get("file_type") == "txt")


# --------------------------------------------------------------------------- #
# 9. JSON 结构兼容
# --------------------------------------------------------------------------- #
def test_json_shapes() -> None:
    print("\n==== 9. JSON 结构兼容 ====")
    loader = DocumentLoader()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        obj_json = tmp_dir / "obj.json"
        obj_json.write_text('{"name": "锅圈", "tags": ["火锅", "食材"]}', encoding="utf-8")
        obj_docs = loader.load_file(obj_json)
        check("顶层对象 → 整篇一条", len(obj_docs) == 1, f"片段数={len(obj_docs)}")
        check(
            "顶层对象内容完整",
            bool(obj_docs) and "锅圈" in obj_docs[0].page_content and "火锅" in obj_docs[0].page_content,
        )

        arr_json = tmp_dir / "arr.json"
        arr_json.write_text('[{"q": "A"}, {"q": "B"}]', encoding="utf-8")
        arr_docs = loader.load_file(arr_json)
        check("顶层数组 → 每条一条", len(arr_docs) == 2, f"片段数={len(arr_docs)}")

        bad_json = tmp_dir / "bad.json"
        bad_json.write_text("{not valid json", encoding="utf-8")
        check("非法 JSON 返回空且不抛异常", loader.load_file(bad_json) == [])


# --------------------------------------------------------------------------- #
# 10. 同名文件检测
# --------------------------------------------------------------------------- #
def test_duplicate_file_names() -> None:
    print("\n==== 10. 同名文件检测 ====")
    loader = DocumentLoader()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        dir_a = tmp_dir / "a"
        dir_b = tmp_dir / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        # 先放两个不同名文件：不应被判为同名冲突
        (dir_a / "alpha.txt").write_text("A 目录的 alpha 内容。" * 30, encoding="utf-8")
        (dir_b / "beta.txt").write_text("B 目录的 beta 内容。" * 30, encoding="utf-8")
        check("不同名文件无冲突", loader.find_duplicate_names(loader.load_directory(tmp_dir)) == {})

        # 再加入两个同名、但内容不同的文件
        (dir_a / "说明.txt").write_text("A 目录的说明内容。" * 30, encoding="utf-8")
        (dir_b / "说明.txt").write_text("B 目录的说明内容。" * 30, encoding="utf-8")

        recorder = WarningRecorder()
        loader_logger = logging.getLogger("core.document_loader")
        loader_logger.addHandler(recorder)
        try:
            docs = loader.load_directory(tmp_dir)
        finally:
            loader_logger.removeHandler(recorder)

        duplicates = loader.find_duplicate_names(docs)
        check("检测到同名文件", set(duplicates) == {"说明.txt"}, str(sorted(duplicates)))
        check(
            "同名文件列出全部来源路径",
            len(duplicates.get("说明.txt", [])) == 2,
            str(duplicates.get("说明.txt")),
        )
        check("同名冲突产生 WARNING 告警", any("同名文件" in m for m in recorder.messages), str(recorder.messages))

        # 两份都必须保留：来源不同、内容都在
        sources = {d.metadata["source"] for d in docs}
        check("两份同名文件都被保留", len(sources) == 4, str(sorted(sources)))
        merged = "".join(d.page_content for d in docs)
        check("两份同名文件内容都在", "A 目录的说明内容" in merged and "B 目录的说明内容" in merged)

        # 同一来源重复出现（如片段被多次传入）不算同名冲突
        one = Document(page_content="x", metadata={"file_name": "n.txt", "source": "/p/n.txt"})
        two = Document(page_content="y", metadata={"file_name": "n.txt", "source": "/p/n.txt"})
        check("同一来源重复不算冲突", loader.find_duplicate_names([one, two]) == {})


def main() -> int:
    test_supported_extensions()
    test_load_single_files()
    test_chunk_params()
    test_load_directory()
    test_edge_cases()
    test_directory_skip_rules()
    test_encoding_fallback()
    test_metadata_override()
    test_json_shapes()
    test_duplicate_file_names()

    print(f"\n==== 汇总：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项 ====")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
