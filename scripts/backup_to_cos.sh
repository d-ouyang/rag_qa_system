#!/usr/bin/env bash
# ==========================================================================
# P2-10b：整站备份 → COS（按量计费，同地域走内网免流量费）
#
# 用法（在服务器上，项目根 /opt/qa）：
#   sudo bash scripts/backup_to_cos.sh              # 全流程（dump + 打包 + 上传）
#   sudo bash scripts/backup_to_cos.sh --public     # 走公网 endpoint（本机调试用）
#
# 前置（缺一不可，见 docs/备份方案-COS.md）：
#   1. COS 桶已建（同地域 ap-shanghai，私有读写）
#   2. .env 已配 COS_BUCKET / COS_REGION / COS_SECRET_ID / COS_SECRET_KEY
#   3. 服务器已装 coscli（腾讯云官方 CLI）
#
# 备什么（真相源分工决定，详见部署方案）：
#   - MySQL     → mysqldump 逻辑导出（named volume 不直接拷；--single-transaction 不锁表）
#   - upload/   → tar（原始文件，解析链路的源头）
#   - chroma_data/ → tar（向量库；虽可从 upload 重灌，但重灌要时间，直接备体积小）
#   不备 redis_data：AOF 非强持久，任务真相在 MySQL document.status（P1-5 定调）。
#
# 保留策略：不在这里删！靠 COS 控制台的「生命周期规则」（前缀 qa-backup/ N 天自动
# 删除）——云端侧配置比脚本删更稳（脚本没跑、密码改了都不会影响旧备份）。
#
# 密钥安全：只从 .env 读；.env 不进 git（P2-8 审计口径）。
# ==========================================================================
set -euo pipefail

# ---- 定位项目根（脚本在 scripts/ 下）----
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- 读 .env（取 COS_* 与 MySQL 口径；.env 的 MYSQL_* 是裸跑口径，
#      容器内用 compose 注入的 env，两者 DATABASE 名一致）----
if [ ! -f "$ROOT/.env" ]; then
    echo "❌ 找不到 $ROOT/.env" >&2; exit 1
fi
set -a; source "$ROOT/.env"; set +a

: "${COS_BUCKET:?请在 .env 配置 COS_BUCKET（格式 桶名-appid）}"
: "${COS_REGION:?请在 .env 配置 COS_REGION（如 ap-shanghai）}"
: "${COS_SECRET_ID:?请在 .env 配置 COS_SECRET_ID}"
: "${COS_SECRET_KEY:?请在 .env 配置 COS_SECRET_KEY}"
: "${MYSQL_DATABASE:=rag_qa}"

PUBLIC_MODE=0
[ "${1:-}" = "--public" ] && PUBLIC_MODE=1

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /tmp/qa-backup-$STAMP-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
LOG_PREFIX="[backup $STAMP]"

# 防重叠：systemd timer 重启/手动误触时，上一轮还没跑完就不开第二轮。
# 锁必须放固定路径（$WORK 每轮不同，锁不到跨轮）。
exec 9>/tmp/qa-backup.lock
flock -n 9 || { echo "$LOG_PREFIX 上一轮备份仍在跑，跳过本次"; exit 0; }

echo "$LOG_PREFIX 开始 | 工作目录 $WORK"

# ---- 1. MySQL 逻辑导出（容器内 mysqldump；密码不落盘不进参数列表）----
MYSQL_FILE="mysql-$STAMP.sql.gz"
echo "$LOG_PREFIX [1/3] mysqldump → $MYSQL_FILE"
docker compose --profile full exec -T mysql sh -c '
    exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
        --single-transaction --routines --triggers --events "$MYSQL_DATABASE"' \
    | gzip > "$WORK/$MYSQL_FILE"

# 导出文件 0 字节 = dump 失败（set -e 抓不到管道 gzip 的成功），必须拦
if [ ! -s "$WORK/$MYSQL_FILE" ]; then
    echo "❌ mysqldump 产出为空，中止（不消灭极可能的真故障）" >&2; exit 1
fi

# ---- 2. upload/ + chroma_data/ 打包（bind mount，宿主机直接读）----
DATA_FILE="data-$STAMP.tar.gz"
echo "$LOG_PREFIX [2/3] tar upload/ + chroma_data/ → $DATA_FILE"
# -h：models 若是软链不跟进（模型不属于备份范围，可重新下载）
tar --exclude='upload/.tmp-*' -czf "$WORK/$DATA_FILE" -C "$ROOT" upload chroma_data

# ---- 3. 上传 COS（同地域默认走内网 endpoint：免外网下行流量费）----
if [ "$PUBLIC_MODE" = "1" ]; then
    ENDPOINT="cos.$COS_REGION.tencentcos.com"
else
    ENDPOINT="cos.$COS_REGION.internal.tencentcos.com"
fi
PREFIX="qa-backup/$(date +%Y/%m/%d)"
echo "$LOG_PREFIX [3/3] 上传 cos://$COS_BUCKET/$PREFIX/（endpoint=$ENDPOINT）"

coscli cp "$WORK/$MYSQL_FILE" "cos://$COS_BUCKET/$PREFIX/$MYSQL_FILE" \
    -e "$ENDPOINT" -i "$COS_SECRET_ID" -k "$COS_SECRET_KEY" --init-skip=true >/dev/null
coscli cp "$WORK/$DATA_FILE" "cos://$COS_BUCKET/$PREFIX/$DATA_FILE" \
    -e "$ENDPOINT" -i "$COS_SECRET_ID" -k "$COS_SECRET_KEY" --init-skip=true >/dev/null

# ---- 摘要 ----
MYSQL_MB=$(du -m "$WORK/$MYSQL_FILE" | cut -f1)
DATA_MB=$(du -m "$WORK/$DATA_FILE" | cut -f1)
echo "$LOG_PREFIX ✅ 完成 | mysql=${MYSQL_MB}MB data=${DATA_MB}MB → cos://$COS_BUCKET/$PREFIX/"
echo "$LOG_PREFIX 校验抽查：coscli ls cos://\$COS_BUCKET/$PREFIX/ 应能看到两个文件"