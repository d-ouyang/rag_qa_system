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

echo "==> [3/6] 数据库迁移 (alembic upgrade head)"
docker compose --profile full run --rm backend alembic upgrade head

echo "==> [4/6] 重建并启动全栈应用（--build 只重建变更服务）"
docker compose --profile full up -d --build

echo "==> [5/6] 清理悬虚镜像（4C4G 磁盘宝贵）"
docker image prune -f

echo "==> [6/6] 等待全部 healthy 并探活"
docker compose --profile full up -d --wait

HEALTH_URL="http://127.0.0.1:8080/api/health"
if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo "✅ 部署成功：$HEALTH_URL 正常"
else
  echo "❌ 健康检查失败，最近日志如下："
  docker compose --profile full logs --tail=60
  exit 1
fi

docker compose --profile full ps
