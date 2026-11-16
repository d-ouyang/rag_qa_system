"""
项目身份信息（名称 / 版本号）的**入库真相源** —— 随代码与 git tag 一起同步。

--------------------------------------------------------------------------
为什么要有这个文件（2026-10-16，PLAN-v4.0.0.md §1.1）
--------------------------------------------------------------------------
版本号原先只放在 `.env` 的 `PROJECT_VERSION`，而 `.env` 被 gitignore ——
`scripts/deploy.sh` 在服务器上是 `git reset --hard origin/main` + `git clean -fd`，
**.env 不会被代码部署带过去**。于是服务器的 `.env` 长期停留在部署那天的旧值
（真实事故：代码已是 3.1.0-p3.7d，UI 设置页与 /system/health 还显示 3.0.0-p3.6）。

--------------------------------------------------------------------------
4.1.0-p4.2c：版本号改为**自动解析**（不再依赖每次迭代手动 bump）
--------------------------------------------------------------------------
上面"入库真相源"方案仍有残留问题：真相源是本文件的常量，靠「迭代五件事」的
纪律保证更新——纪律一松（4.1.0 全程没 bump），UI 就又显示旧版本
（线上实锤 2026-11-16：生产已 4.1.0-p4.2，侧栏还写着 4.0.0-p4.16）。

现在的解析顺序（`PROJECT_VERSION` 在 import 时求值一次）：

    1. `config/_runtime_version.txt` —— deploy.sh 在 `git reset --hard` 之后
       用 `git describe --tags` 生成，再 build 镜像烧进去。生产唯一生效路径，
       与「部署的到底是哪个 tag」逐字对齐，**部署即正确，无需任何手动步骤**。
    2. `git describe --tags --always` —— 本地开发（源码目录就是 git 仓库）。
    3. 下面的常量 —— 兜底（非 git 环境；保持与最近一次 tag 一致即可）。
"""

import subprocess
from pathlib import Path

# 项目显示名。.env / 服务器 .env 亦可覆盖（覆盖优先级更高）。
PROJECT_NAME = "企业级RAG智能问答系统"

# 兜底常量：只在非 git 环境（且没有部署生成的 runtime 文件）时生效。
# 语义从「真相源」降级为「最后防线」，不再要求每次迭代手动 bump。
_FALLBACK_VERSION = "4.1.0-p4.2"

_RUNTIME_FILE = Path(__file__).resolve().parent / "_runtime_version.txt"


def _version_from_runtime_file() -> str:
    """deploy.sh 在服务器上生成的版本文件（烧进镜像，生产唯一真相源）。"""
    try:
        value = _RUNTIME_FILE.read_text(encoding="utf-8").strip()
        return value
    except OSError:
        return ""


def _version_from_git() -> str:
    """本地开发：源码目录即 git 仓库，直接问 git。失败（非仓库/无 git）返回空串。"""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip().lstrip("v")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _resolve_version() -> str:
    return _version_from_runtime_file() or _version_from_git() or _FALLBACK_VERSION


PROJECT_VERSION = _resolve_version()
