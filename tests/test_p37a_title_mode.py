# pyright: basic
"""
P3-7a 测试：title 切分模式与 Enrichment（core/document_loader.py / core/vector_store.py）

运行：
    .venv/bin/python tests/test_p37a_title_mode.py     （已挂进 make test / test-sandbox）

覆盖点：
1. fixed 模式零变化：不产 header_path / metadata["content"]，行为与 P3-7a 之前一致
2. title 模式：header_path 正确、metadata["content"] 是原文、page_content 是 enriched
3. 短节向相邻合并、长节二次切分
4. 表格原子性：markdown 表格绝不从中间切断（B 阶段 MinerU markdown 也走这条路）
5. 读取侧 _display_content / _chunk_payload：引用反查返回原文，不返回 enriched 拼接串
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core.document_loader import DocumentLoader
from core.vector_store import VectorStoreManager

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


SAMPLE_MD = """# 员工手册

## 考勤制度

工作时间为周一至周五，上午九点到下午六点。{FILLER}

### 迟到处理

迟到三十分钟以内扣款五十元。{FILLER_SHORT}

## 报销制度

{LONG_SECTION}

## 采购流程

| 物资类别 | 审批人 | 时限 |
| --- | --- | --- |
| 办公用品 | 行政主管 | 3 个工作日 |
| 电子设备 | IT 负责人 | 5 个工作日 |
| 差旅预订 | 部门经理 | 2 个工作日 |
| 培训费用 | HR 负责人 | 7 个工作日 |
| 团建费用 | 部门经理 | 5 个工作日 |
| 客户礼品 | 销售总监 | 4 个工作日 |
| 法务咨询 | 法务负责人 | 6 个工作日 |
| 其他事项 | 总经理 | 10 个工作日 |

采购完成后需在 OA 系统归档。
"""

_FILLER = "本公司实行标准工时制度，遇法定节假日按国家规定安排。" * 10
_LONG_SECTION = (
    "报销需遵循以下详细规定。" + "员工提交报销单据后，先由直属上级审批，再由财务复核，最后由总经理签批。"
    + "单张发票超过一千元的，需附情况说明。" * 30
)


def _write_sample(tmp: Path) -> Path:
    text = SAMPLE_MD.replace("{FILLER}", _FILLER).replace("{FILLER_SHORT}", "").replace(
        "{LONG_SECTION}", _LONG_SECTION)
    p = tmp / "title_mode_sample.md"
    p.write_text(text, encoding="utf-8")
    return p


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        sample = _write_sample(Path(td))
        base_meta = {"source": str(sample), "file_name": sample.name, "file_type": "md"}

        # ---- 1. fixed 模式零变化（显式置 fixed，不依赖 .env 现值）----
        saved_mode_init = settings.CHUNK_MODE
        try:
            settings.CHUNK_MODE = "fixed"
            loader_fixed = DocumentLoader()
            docs_fixed = loader_fixed.load_file(sample, metadata={"doc_id": 1, "project_id": "t"})
        finally:
            settings.CHUNK_MODE = saved_mode_init
        check("fixed：正常出片", len(docs_fixed) > 0)
        check("fixed：无 header_path", all("header_path" not in d.metadata for d in docs_fixed))
        check("fixed：无 metadata content", all("content" not in d.metadata for d in docs_fixed))

        # ---- 2~4. title 模式 ----
        saved_mode = settings.CHUNK_MODE
        try:
            settings.CHUNK_MODE = "title"
            loader_title = DocumentLoader()
            docs_title = loader_title.load_file(sample, metadata={"doc_id": 2, "project_id": "t"})
        finally:
            settings.CHUNK_MODE = saved_mode

        check("title：正常出片", len(docs_title) > 0, f"出片 {len(docs_title)}")
        check("title：全部带 header_path", all(d.metadata.get("header_path") for d in docs_title))
        check("title：header_path 含章名",
              any("考勤制度" in str(d.metadata.get("header_path")) for d in docs_title))
        # enriched：page_content 以 header_path 开头
        check("title：page_content 为 enriched（含 header 前缀）",
              all(str(d.metadata.get("header_path")) in d.page_content for d in docs_title))
        # 原文保留：metadata content 不含 header_path 前缀，且与 page_content 去前缀后一致
        ok_orig = True
        for d in docs_title:
            hp = str(d.metadata.get("header_path") or "")
            orig = str(d.metadata.get("content") or "")
            if not orig or orig in d.page_content is False:
                ok_orig = False
            if hp and d.page_content != f"{hp}\n{orig}" and not d.page_content.endswith(orig):
                ok_orig = False
        check("title：metadata content 与 page_content 同源", ok_orig)
        # 原文无路径前缀
        check("title：原文不含 ' > ' 路径前缀",
              all(" > " not in str(d.metadata.get("content"))[:20] for d in docs_title))

        # ---- 3. 短节合并：「迟到处理」只有一句话（<150 字），应并入前面的考勤制度块 ----
        short_merged = [
            d for d in docs_title
            if "迟到三十分钟以内" in str(d.metadata.get("content"))
        ]
        check("title：短节文本仍在库", bool(short_merged))
        check("title：短节并入相邻块（header 沿用考勤制度，不再单挂迟到处理）",
              bool(short_merged) and short_merged[0].metadata.get("header_path") == "员工手册 > 考勤制度",
              f"实际 header_path={[str(d.metadata.get('header_path')) for d in short_merged]}")

        # ---- 4. 表格原子性 ----
        table_chunk = [
            d for d in docs_title
            if "| 办公用品 |" in d.page_content
        ]
        check("title：表格所在片段存在", bool(table_chunk))
        if table_chunk:
            whole = table_chunk[0].page_content
            rows_present = all(k in whole for k in ("办公用品", "电子设备", "法务咨询", "其他事项"))
            check("title：表格整块在一个片段（首尾行同片）", rows_present)
            halves = [
                d for d in docs_title
                if ("| 办公用品 |" in d.page_content) != ("| 其他事项 |" in d.page_content)
                and "| 审批人 |" in d.page_content
            ]
            check("title：表格未被拦腰切开", len(halves) == 0)

        # ---- 4b. 超长表按行组切：行不拦腰断、表头重复（嵌入模型输入上限的现实约束）----
        big_table_md = (
            "# 物价表\n\n| 物品 | 价格 | 备注 |\n| --- | --- | --- |\n"
            + "".join(f"| 物品编号{i} | {i * 3} 元 | 备注说明第{i}行，附额外说明文字若干 |\n"
                      for i in range(40))
        )
        big_path = Path(td) / "big_table.md"
        big_path.write_text(big_table_md, encoding="utf-8")
        saved_mode2 = settings.CHUNK_MODE
        try:
            settings.CHUNK_MODE = "title"
            docs_big = DocumentLoader().load_file(
                big_path, metadata={"doc_id": 3, "project_id": "t"})
        finally:
            settings.CHUNK_MODE = saved_mode2
        table_pieces = [d for d in docs_big if "| 物品 | 价格 |" in d.page_content]
        check("title：超长表切成多个行组分片", len(table_pieces) >= 2,
              f"分片数 {len(table_pieces)}")
        check("title：每个行组分片都带表头",
              all("| 物品编号" in p.page_content for p in table_pieces))
        # 没有任何一行被拦腰断：所有行都完整出现在某个分片里
        all_rows_intact = all(
            any(f"| 物品编号{i} |" in p.page_content for p in docs_big)
            for i in range(40)
        )
        check("title：40 行全部完整（无拦腰断行）", all_rows_intact)
        check("title：行组分片 ≤ chunk_size（嵌入上限内）",
              all(len(p.page_content) <= 600 for p in docs_big),
              f"最长 {max(len(p.page_content) for p in docs_big)}")

        # ---- 4c. 开头连续短节：逐个累加不覆盖，内容零丢失（p3.7a 自测抓到的真 bug）----
        many_short_md = (
            "# 指引文档\n\n## 一、第一节\n\n内容一句话很短。\n\n"
            "## 二、第二节\n\n也只有一句话。\n\n## 三、第三节\n\n还是很短。\n\n"
            "## 四、长节\n\n" + _LONG_SECTION
        )
        short_path = Path(td) / "many_short.md"
        short_path.write_text(many_short_md, encoding="utf-8")
        saved_mode3 = settings.CHUNK_MODE
        try:
            settings.CHUNK_MODE = "title"
            docs_short = DocumentLoader().load_file(
                short_path, metadata={"doc_id": 4, "project_id": "t"})
        finally:
            settings.CHUNK_MODE = saved_mode3
        full_text = "\n".join(str(d.metadata.get("content")) for d in docs_short)
        for key in ("第一节", "第二节", "第三节", "长节"):
            check(f"title：连续短节内容不丢（{key}）", key in full_text)
        check("title：合并块 header 为公共祖先",
              all(str(d.metadata.get("header_path")) == "指引文档" for d in docs_short),
              f"实际 {[str(d.metadata.get('header_path')) for d in docs_short]}")

        # ---- 5. 读取侧：引用反查返回原文 ----
        enriched_text = "员工手册 > 报销制度\n报销需遵循以下详细规定。"
        original_text = "报销需遵循以下详细规定。"
        meta = {"content": original_text, "header_path": "员工手册 > 报销制度", "page": None,
                "project_id": "t", "source": "s", "file_name": "f.md", "file_type": "md"}
        payload = VectorStoreManager._chunk_payload(9, 0, enriched_text, meta)
        check("读取：_chunk_payload 返回原文", payload["content"] == original_text)
        check("读取：char_count 按原文计", payload["char_count"] == len(original_text))
        check("读取：header_path 透出", payload["header_path"] == "员工手册 > 报销制度")
        # fixed/老切片（无 content 字段）回退存储文本
        payload_old = VectorStoreManager._chunk_payload(9, 1, "老切片正文", {"page": 1})
        check("读取：无 content 字段时回退存储文本", payload_old["content"] == "老切片正文")
        check("读取：回退时 header_path 为 None", payload_old["header_path"] is None)

    print()
    print(f"P3-7a：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
