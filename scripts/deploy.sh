#!/usr/bin/env bash
#
# 服务器自托管部署脚本：由 GitHub Actions（self-hosted runner）在 /opt/qa 调用。
# 不依赖 actions/checkout —— 直接操作已有的 git 仓库 + 本地 docker。
#
# 关键安全约束：
#   · 绝不使用 `docker compose down -v`（会删 volume / 业务数据）。
#   · 只动被 git 跟踪的文件；.env / upload / chroma_data 均被 .gitignore 排除，不会被碰。
#   · 迁移只前向升级（alembic upgrade head），幂等。
#
# 部署提速（2026-10-22）：
#   · 确保 buildx 可用（并行构建 + 内容寻址缓存，替代退化的旧构建器）。
#   · 镜像只 build 一次（step 4 全量 build，step 6 只起容器不再重建）。
#   · prune 保留 24h 内镜像，避免误清刚构建好的依赖缓存层。
#
set -euo pipefail

REPO_DIR="/opt/qa"
cd "$REPO_DIR"

echo "==> [1/9] 拉取最新代码 (origin/main)"
git fetch --all --prune
git reset --hard origin/main
# 清掉未跟踪但非忽略的文件（构建残留等）；被 .gitignore 的 .env/models/upload/chroma_data 不受影响
git clean -fd

echo "==> [2/9] 确保中间件（mysql/redis/chroma）起来并健康"
docker compose up -d mysql redis chroma
docker compose up -d --wait mysql redis chroma

echo "==> [3/9] 确保 buildx 可用（构建提速：并行 + 内容寻址缓存）"
ensure_buildx() {
  if docker buildx version >/dev/null 2>&1; then
    echo "  buildx 已可用（compose 将使用 buildx 并行构建 + 内容寻址缓存）"
    return 0
  fi
  echo "  buildx 未安装，尝试安装 CLI 插件（best-effort，失败则退回旧构建器）..."
  # 1) 优先 apt（需 sudo，可能无密码 sudo）
  if command -v apt-get >/dev/null 2>&1; then
    if sudo -n apt-get update -qq >/dev/null 2>&1 && \
       sudo -n apt-get install -y -qq docker-buildx-plugin >/dev/null 2>&1; then
      echo "  ✅ 已通过 apt 安装 docker-buildx-plugin"
      return 0
    fi
  fi
  # 2) 回退：下载到用户级 cli-plugins（无需 sudo）
  # ⚠️ 两处 curl 都给**短超时**：服务器实测无外网，不加超时会白等 ~98s（2026-10-22 实测）。
  # ⚠️ `ver=$(...) || ver=""` 是必须的：命令替换失败时返回非 0，在 `set -euo pipefail`
  #    下会**直接把整个部署打断** —— 那就不是「装不上 buildx」，而是「部署失败」。
  #    best-effort 的每一步都必须自己吞掉错误，不能指望调用方。
  mkdir -p ~/.docker/cli-plugins 2>/dev/null || true
  local ver
  ver=$(
    curl -fsSL --connect-timeout 5 --max-time 12 \
      https://api.github.com/repos/docker/buildx/releases/latest 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('tag_name',''))" 2>/dev/null
  ) || ver=""
  if [ -n "$ver" ]; then
    if curl -fsSL --connect-timeout 5 --max-time 25 \
         "https://github.com/docker/buildx/releases/download/${ver}/buildx-${ver}.linux-amd64" \
         -o ~/.docker/cli-plugins/docker-buildx 2>/dev/null; then
      chmod +x ~/.docker/cli-plugins/docker-buildx
      echo "  ✅ 已下载 buildx ${ver} 到用户目录"
      return 0
    fi
  fi
  echo "  ⚠️ 无法安装 buildx（服务器无外网 / 无免密 sudo），使用默认构建器 ——"
  echo "     依赖缓存由 buildkit 的 docker driver 提供，实测已生效（本次构建大量 CACHED）。"
  return 0
}
ensure_buildx

echo "==> [4/9] 构建全部镜像（迁移必须用新镜像：代码烧进镜像、backend 没挂代码卷）"
# ⚠️ 顺序不能反（2026-10-14 修复）：旧版先迁移后 build，而 `compose run` 不触发
# 构建 —— 迁移跑在**旧镜像**上，只认识上一个版本的 alembic/versions，
# 新迁移被静默跳过（upgrade head 对旧镜像而言已经是 head）。
# 必须先 build 出含新迁移的镜像，再在它里面跑 alembic。
# 一次性 build 全部服务（backend/worker 共用镜像、gateway、frontend），step 6 起容器时不再重建。
# 依赖层（torch 等）已锁 base digest + 独立 deps 阶段，仅首版（或 requirements.lock.txt 变更）才重装。
docker compose --profile full build

echo "==> [5/9] 数据库迁移 (alembic upgrade head，在新镜像内执行)"
docker compose --profile full run --rm backend alembic upgrade head

echo "==> [6/9] 启动全栈应用（镜像已在 step 4 build，这里只起容器，不重建）"
docker compose --profile full up -d

echo "==> [7/9] 清理悬虚镜像（4C4G 磁盘宝贵，但保留 24h 内镜像以保护刚构建的依赖缓存）"
# ⚠️ 必须写成 --filter "until=24h"：docker image prune **没有** --until 这个 flag。
#   2026-10-22 踩过：写成 `--until 24h` → unknown flag、exit 125，把整个部署打成失败
#   （当时容器其实已经重建完成、健康，纯粹栽在这一行上）。
docker image prune -f --filter "until=24h"

echo "==> [8/9] 等待全部 healthy 并探活"
docker compose --profile full up -d --wait

echo "==> [9/9] 构建并发布 admin-console 静态产物"
# admin-console 不在 docker 镜像体系里：生产形态是静态文件（/var/www/admin，Caddy file_server）。
# 服务器宿主机没有 node —— 用一次性 node 容器构建（node:22-alpine 与 gateway 大版本一致）；
# npm 缓存挂 named volume，二次部署明显提速。静态站点失败不拖垮服务部署（只报错不 exit）。
ADMIN_DIR="$REPO_DIR/admin-console"
if [ -d "$ADMIN_DIR" ]; then
  if docker run --rm \
      -v "$ADMIN_DIR":/app -w /app \
      -v admin_npm_cache:/root/.npm \
      node:22-alpine sh -c "npm ci --no-audit --no-fund && npm run build"; then
    mkdir -p /var/www/admin
    rsync -a --delete "$ADMIN_DIR/dist/" /var/www/admin/
    chmod -R a+rX /var/www/admin
    echo "✅ admin-console 已发布到 /var/www/admin"
  else
    echo "❌ admin-console 构建失败（服务不受影响，但 admin.nianan.site 静态页面未更新）"
  fi
else
  echo "跳过：无 admin-console 目录"
fi

HEALTH_URL="http://127.0.0.1:8080/api/health"
if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo "✅ 部署成功：$HEALTH_URL 正常"
else
  echo "❌ 健康检查失败，最近日志如下："
  docker compose --profile full logs --tail=60
  exit 1
fi

docker compose --profile full ps
