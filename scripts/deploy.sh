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
set -euo pipefail

REPO_DIR="/opt/qa"
cd "$REPO_DIR"

echo "==> [1/6] 拉取最新代码 (origin/main)"
git fetch --all --prune
git reset --hard origin/main
# 清掉未跟踪但非忽略的文件（构建残留等）；被 .gitignore 的 .env/models/upload/chroma_data 不受影响
git clean -fd

echo "==> [2/6] 确保中间件（mysql/redis/chroma）起来并健康"
docker compose up -d mysql redis chroma
docker compose up -d --wait mysql redis chroma

echo "==> [3/6] 构建镜像（迁移必须用新镜像：代码烧进镜像、backend 没挂代码卷）"
# ⚠️ 顺序不能反（2026-10-14 修复）：旧版先迁移后 build，而 `compose run` 不触发
# 构建 —— 迁移跑在**旧镜像**上，只认识上一个版本的 alembic/versions，
# 新迁移被静默跳过（upgrade head 对旧镜像而言已经是 head）。
# 必须先 build 出含新迁移的镜像，再在它里面跑 alembic。
docker compose --profile full build backend

echo "==> [4/6] 数据库迁移 (alembic upgrade head，在新镜像内执行)"
docker compose --profile full run --rm backend alembic upgrade head

echo "==> [5/6] 重建并启动全栈应用（镜像已 build，这里只起容器）"
docker compose --profile full up -d --build

echo "==> [6/6] 清理悬虚镜像（4C4G 磁盘宝贵）"
docker image prune -f

echo "==> [7/7] 等待全部 healthy 并探活"
docker compose --profile full up -d --wait

echo "==> [8/8] 构建并发布 admin-console 静态产物"
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
