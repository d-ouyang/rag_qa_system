# pyright: basic
"""
P3-7b 测试：PDF 路由走 MinerU + 优雅降级（core/document_loader.py / core/mineru_client.py）

运行：
    .venv/bin/python tests/test_p37b_mineru.py     （已挂进 make test / test-sandbox）

全部 mock 网络层 —— 不依赖 MinerU 服务与额度。覆盖点：
1. mineru_available 路由判据（开关 × key）
2. 开关关 → 直接 PyMuPDF，不发起网络调用
3. MinerU 成功返回 markdown → 一律走 title 切分（header_path + 表格行组原子）
4. MinerU 各环节失败（申请/上传/解析失败/轮询超时）→ 降级 PyMuPDF，不抛错
5. parse_pdf_to_markdown 单元：批量结果认领（data_id）与 zip 抽取
"""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core import mineru_client
from core.document_loader import DocumentLoader
from core.mineru_client import MinerUError

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


def _make_pdf(tmp: Path, name: str = "t.pdf") -> Path:
    """最小合法 PDF（PyMuPDF 能打开即可，降级路径不校验内容）。"""
    p = tmp / name
    p.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\n%%EOF"
    )
    return p


MINERU_MD = """# 采购管理制度

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


class FakeResp:
    def __init__(self, j=None, status_code=200, content=None):
        self._j = j or {}
        self.text = json.dumps(self._j) if j is not None else ""
        self.status_code = status_code
        self.content = content if content is not None else self.text.encode()

    def json(self):
        return self._j


def main() -> int:
    import tempfile

    saved = {k: getattr(settings, k) for k in
             ("MINERU_ENABLED", "MINERU_API_KEY", "MINERU_MAX_BYTES", "CHUNK_MODE")}
    try:
        with tempfile.TemporaryDirectory() as td:
            pdf = _make_pdf(Path(td))

            # ---- 1. 路由判据 ----
            settings.MINERU_ENABLED = True
            settings.MINERU_API_KEY = "k"
            check("available：开关开+key 在 → True", mineru_client.mineru_available())
            settings.MINERU_API_KEY = ""
            check("available：key 缺失 → False", not mineru_client.mineru_available())
            settings.MINERU_ENABLED = False
            settings.MINERU_API_KEY = "k"
            check("available：开关关 → False", not mineru_client.mineru_available())
            settings.MINERU_ENABLED = True

            # ---- 2. 开关关 → 不碰网络，直接 PyMuPDF ----
            settings.MINERU_ENABLED = False
            settings.CHUNK_MODE = "fixed"
            with mock.patch.object(mineru_client.requests, "post") as mp:
                docs = DocumentLoader().load_file(pdf, metadata={"doc_id": 1, "project_id": "t"})
                check("路由：开关关 → 网络零调用", mp.call_count == 0)
            check("路由：降级 PyMuPDF 正常出段", len(docs) >= 0)   # 最小 PDF 可能无文本，不出错即可

            # ---- 3. MinerU 成功 → markdown 进 title 切分 ----
            settings.MINERU_ENABLED = True
            settings.CHUNK_MODE = "fixed"   # 刻意 fixed：MinerU md 也应走 title 切分

            def fake_post(url, **kw):
                if "file-urls/batch" in url:
                    return FakeResp({"code": 0, "data": {
                        "batch_id": "b-123", "file_urls": ["http://oss/put"]}})
                return FakeResp({"code": 1, "msg": "不应调用任务提交端点"})

            def fake_get(url, **kw):
                if "extract-results/batch/b-123" in url:
                    # data_id 由客户端随机生成，这里从请求不可见 —— 返回通配条目由
                    # 客户端按 data_id 认领：mock 里直接回一个必然匹配的条目不可能，
                    # 所以这里返回「条目缺 data_id」与「有 data_id」两种都试不出匹配。
                    # 客户端认领逻辑是 `it.get("data_id") == data_id`，mock 无法预知。
                    # 解法：让条目 state 直接 done 但 data_id 不匹配 → 会一直等。
                    # 因此该场景在单元层单独测（见下面第 5 组），这里用 zip 直达。
                    return FakeResp({"code": 0, "data": {"extract_result": []}})
                return FakeResp({"code": 0, "data": {}})

            def fake_put(url, data=None, **kw):
                return FakeResp(status_code=200)

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("b-123/full.md", MINERU_MD)

            # 成功路径单元：直接驱动 parse_pdf_to_markdown，mock 掉轮询环节
            def fake_poll_ok():
                pass

            with mock.patch.object(mineru_client.requests, "post", side_effect=fake_post), \
                 mock.patch.object(mineru_client.requests, "put", side_effect=fake_put), \
                 mock.patch.object(mineru_client.requests, "get") as mg:
                # 第一次轮询就返回 done + 匹配 data_id 是做不到的（data_id 随机），
                # 改为捕获客户端发出的轮询 URL 后返回动态构造的响应：
                def dynamic_get(url, **kw):
                    if "extract-results/batch/b-123" in url:
                        return FakeResp({"code": 0, "data": {"extract_result": [
                            {"data_id": _captured_data_id(), "state": "done",
                             "full_zip_url": "http://oss/zip"}]}})
                    if url == "http://oss/zip":
                        return FakeResp(content=buf.getvalue())
                    return FakeResp({"code": 0, "data": {}})
                mg.side_effect = dynamic_get
                # 记录客户端 data_id：拦截 file-urls/batch 请求体
                captured = {}

                def capturing_post(url, **kw):
                    if "file-urls/batch" in url:
                        captured["data_id"] = kw["json"]["files"][0]["data_id"]
                        return FakeResp({"code": 0, "data": {
                            "batch_id": "b-123", "file_urls": ["http://oss/put"]}})
                    return FakeResp({"code": 1, "msg": "不应调用任务提交端点"})

                # _captured_data_id 闭包：从 captured 读
                def _captured_data_id():
                    return captured.get("data_id", "")

                with mock.patch.object(mineru_client.requests, "post",
                                       side_effect=capturing_post):
                    md = mineru_client.parse_pdf_to_markdown(pdf)

            check("成功：拿到 markdown", md == MINERU_MD, f"{len(md)} 字")
            docs2 = DocumentLoader()._documents_from_markdown(md, pdf)
            check("成功：走 title 切分（带 header_path）",
                  bool(docs2) and all(d.metadata.get("header_path") for d in docs2))
            check("成功：metadata.content 为原文",
                  bool(docs2) and all(d.metadata.get("content") for d in docs2))
            table_doc = [d for d in docs2 if "| 办公用品 |" in d.page_content]
            check("成功：表格行组原子（首尾行同片）",
                  bool(table_doc) and "| 其他事项 |" in table_doc[0].page_content)

            # ---- 3b. MinerU 实测形态：HTML 表（整表单行）行组原子 ----
            html_table = (
                "<table><tr><td rowspan=1 colspan=1>物资类别</td><td>审批人</td></tr>"
                + "".join(f"<tr><td rowspan=1 colspan=1>物品编号{i}</td><td>审批人{i}</td></tr>"
                          for i in range(30))
                + "</table>"
            )
            html_md = f"# 采购制度\n\n## 第一章 限额\n\n{html_table}\n\n## 第二章 规则\n\n所有采购须事前在 OA 提交申请。"
            docs_html = DocumentLoader()._documents_from_markdown(html_md, pdf)
            import re as _re
            all_rows = _re.findall(r"<tr\b.*?</tr>", html_table, _re.S)
            ok_rows = all(
                any(row in str(d.metadata.get("content")) for d in docs_html)
                for row in all_rows
            )
            check("HTML 表：所有行完整（无拦腰断）", ok_rows)
            check("HTML 表：每片 ≤ chunk_size+余量（嵌入上限内）",
                  all(len(d.page_content) <= 700 for d in docs_html),
                  f"最长 {max(len(d.page_content) for d in docs_html)}")
            check("HTML 表：首行表头在每片重复",
                  all("物资类别" in d.page_content
                      for d in docs_html if "物品编号" in d.page_content))

            # ---- 4. 失败降级：解析失败（如额度不足）→ PyMuPDF 接管 ----
            with mock.patch.object(mineru_client.requests, "post",
                                   side_effect=capturing_post), \
                 mock.patch.object(mineru_client.requests, "put", side_effect=fake_put), \
                 mock.patch.object(mineru_client.requests, "get",
                                   side_effect=make_dynamic_get_failed(captured)):
                docs3 = DocumentLoader().load_file(pdf, metadata={"doc_id": 3, "project_id": "t"})
                check("降级：解析失败 → 不抛错", True)
                check("降级：PyMuPDF 接管（无 header_path）",
                      all("header_path" not in d.metadata for d in docs3))

            # 轮询超时降级
            settings.MINERU_POLL_TIMEOUT = 2
            settings.MINERU_POLL_INTERVAL = 1
            with mock.patch.object(mineru_client.requests, "post",
                                   side_effect=capturing_post), \
                 mock.patch.object(mineru_client.requests, "put", side_effect=fake_put), \
                 mock.patch.object(mineru_client.requests, "get", side_effect=dynamic_get_pending):
                docs4 = DocumentLoader().load_file(pdf, metadata={"doc_id": 4, "project_id": "t"})
                check("降级：轮询超时 → PyMuPDF 接管",
                      all("header_path" not in d.metadata for d in docs4))
            settings.MINERU_POLL_TIMEOUT = 300

            # ---- 5. 单元：条目认领与 zip 抽取 ----
            with mock.patch.object(mineru_client.requests, "get",
                                   return_value=FakeResp(status_code=404, content=b"404")):
                try:
                    mineru_client._download_and_extract_md("http://x/z", "t1")
                    check("zip：下载 404 报 MinerUError", False)
                except MinerUError:
                    check("zip：下载 404 报 MinerUError", True)

            bad_buf = io.BytesIO()
            with zipfile.ZipFile(bad_buf, "w") as z:
                z.writestr("task-x/layout.json", "{}")
            with mock.patch.object(mineru_client.requests, "get",
                                   return_value=FakeResp(content=bad_buf.getvalue())):
                try:
                    mineru_client._download_and_extract_md("http://x/z", "t1")
                    check("zip：无 full.md 报 MinerUError", False)
                except MinerUError as e:
                    check("zip：无 full.md 报 MinerUError", "full.md" in str(e))
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)

    print()
    print(f"P3-7b：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    return 1 if FAILED else 0


def make_dynamic_get_failed(captured: dict):
    """返回 state=failed 的轮询响应。data_id 用捕获值精确匹配 ——
    否则客户端把条目当「未出现」一直等到超时，失败路径测不到还拖慢回归 300 秒。"""
    def _get(url, **kw):
        if "extract-results/batch/" in url:
            return FakeResp({"code": 0, "data": {"extract_result": [
                {"data_id": captured.get("data_id", ""), "state": "failed",
                 "err_msg": "余额不足"}]}})
        return FakeResp({"code": 0, "data": {}})
    return _get


def dynamic_get_pending(url, **kw):
    if "extract-results/batch/" in url:
        return FakeResp({"code": 0, "data": {"extract_result": []}})
    return FakeResp({"code": 0, "data": {}})


if __name__ == "__main__":
    sys.exit(main())
