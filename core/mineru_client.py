"""
MinerU 客户端 —— PDF → 保留表格结构的 markdown（v3.1.0 P3-7b）。

--------------------------------------------------------------------------
定位与边界
--------------------------------------------------------------------------
只做一件事：把一个本地 PDF 变成 MinerU 版面模型输出的 markdown。
上传链路的**可用性不绑定在这里**：本模块抛出的任何异常都会被
`DocumentLoader` 的 PDF 路由接住并降级回 PyMuPDF（见 document_loader._load_pdf），
调用方永远不需要为「MinerU 挂了」写额外逻辑。

--------------------------------------------------------------------------
官方正确流程（2026-10-14 对照 mineru.net API 文档核实，p3.7b 实测踩坑）
--------------------------------------------------------------------------
    1. POST /file-urls/batch        申请 batch_id + presigned 上传链接
    2. PUT  文件到 OSS
    3. —— 无需提交任务！系统自动扫描已上传文件并提交解析 ——
    4. GET  /extract-results/batch/{batch_id}   轮询（注意是 extract-**results**）
    5. done → 下载 full_zip_url，取 full.md

⚠️ 桌面 langChain 项目的 doc_tools.py 把 presigned URL 再提交给 /extract/task ——
那个端点只收**公网可读**的 URL，读不动 MinerU 自家的 OSS 签名链接，
必然得到 "failed to read file"（p3.7b 实测复现后才对照文档纠正）。
本地文件只走上面这条 batch 流。

额度说明：账号按天计额度，额度不足时任务 failed 并在 err_msg 说明 ——
降级路径自然接住，不需要预检（预检有竞态，省不掉失败分支）。
"""
from __future__ import annotations

import io
import logging
import time
import uuid
import zipfile
from pathlib import Path

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


class MinerUError(RuntimeError):
    """MinerU 解析失败。message 进日志并触发降级，不直接给用户。"""


def mineru_available() -> bool:
    """路由判据：开关开 + key 非空。额度/网络问题留给运行时降级（预检有竞态）。"""
    return bool(settings.MINERU_ENABLED and settings.MINERU_API_KEY)


def _headers(json_: bool = True) -> dict:
    h = {"Authorization": f"Bearer {settings.MINERU_API_KEY}"}
    if json_:
        h["Content-Type"] = "application/json"
    return h


def _base() -> str:
    return settings.MINERU_API_BASE.rstrip("/")


def parse_pdf_to_markdown(path: Path) -> str:
    """
    本地 PDF → MinerU 解析后的 markdown（含表格/公式/跨页拼接）。

    :raises MinerUError: 任何一步失败（网络/额度/超时/结构异常）。
                         调用方（document_loader）负责降级，本函数不降级。
    """
    if not path.is_file():
        raise MinerUError(f"文件不存在：{path}")

    # ---- 1. 申请 batch_id + presigned 上传链接 ----
    # data_id 用 uuid：结果列表里靠它认领自己的条目（file_name 可能重名）
    data_id = f"ragqa-{uuid.uuid4().hex[:16]}"
    try:
        r = requests.post(
            f"{_base()}/file-urls/batch",
            headers=_headers(),
            json={
                # model_version 在申请链接时就要带上（自动提交沿用该配置）
                "model_version": settings.MINERU_MODEL_VERSION,
                "enable_formula": True,
                "enable_table": True,
                "files": [{"name": path.name, "data_id": data_id, "is_ocr": False}],
            },
            timeout=15,
        )
        j = r.json() if r.text else {}
    except requests.RequestException as e:
        raise MinerUError(f"申请上传链接失败（网络）：{e}") from e
    if j.get("code") != 0:
        raise MinerUError(f"申请上传链接失败：{j.get('msg', r.text[:200])}")
    data = j.get("data") or {}
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise MinerUError("申请上传链接失败：未返回 batch_id/file_urls")

    # ---- 2. 上传文件（无须 Content-Type；传完系统自动提交解析）----
    try:
        with path.open("rb") as f:
            put = requests.put(urls[0], data=f, timeout=120)
    except requests.RequestException as e:
        raise MinerUError(f"上传文件失败（网络）：{e}") from e
    if put.status_code not in (200, 204):
        raise MinerUError(f"上传文件失败：HTTP {put.status_code}")

    # ---- 3. 轮询批量结果（extract-results，不是 extract/task）----
    deadline = time.time() + settings.MINERU_POLL_TIMEOUT
    last_state = "waiting-file"
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{_base()}/extract-results/batch/{batch_id}",
                headers=_headers(json_=False),
                timeout=10,
            )
            j = r.json() if r.text else {}
        except requests.RequestException as e:
            raise MinerUError(f"轮询失败（网络）：{e}") from e
        if j.get("code") != 0:
            raise MinerUError(f"轮询失败：{j.get('msg', r.text[:200])}")
        items = (j.get("data") or {}).get("extract_result") or []
        mine = next((it for it in items if it.get("data_id") == data_id), None)
        if mine is None:
            # 条目未出现：还没被扫描提交，继续等
            time.sleep(settings.MINERU_POLL_INTERVAL)
            continue
        last_state = mine.get("state", "pending")
        if last_state == "done":
            zip_url = mine.get("full_zip_url")
            if not zip_url:
                raise MinerUError("任务完成但未返回 full_zip_url")
            return _download_and_extract_md(zip_url, batch_id)
        if last_state == "failed":
            raise MinerUError(f"解析失败：{mine.get('err_msg', '未知错误')}")
        time.sleep(settings.MINERU_POLL_INTERVAL)
    raise MinerUError(
        f"轮询超时（>{settings.MINERU_POLL_TIMEOUT}s），最后状态={last_state}"
    )


def _download_and_extract_md(zip_url: str, task_id: str) -> str:
    """下载结果 zip 并抽取 full.md。zip 里还可能有 layout.json 等，只取需要的。"""
    try:
        r = requests.get(zip_url, timeout=60)
    except requests.RequestException as e:
        raise MinerUError(f"下载结果失败（网络）：{e}") from e
    if r.status_code != 200:
        raise MinerUError(f"下载结果失败：HTTP {r.status_code}")
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                if name.rsplit("/", 1)[-1] == "full.md":
                    md = z.read(name).decode("utf-8", errors="replace")
                    if md.strip():
                        logger.info(
                            "MinerU 解析完成 | batch_id=%s | markdown %d 字",
                            task_id, len(md),
                        )
                        return md
    except zipfile.BadZipFile as e:
        raise MinerUError(f"结果 zip 解析失败：{e}") from e
    raise MinerUError(f"结果 zip 中没有 full.md | batch_id={task_id}")
