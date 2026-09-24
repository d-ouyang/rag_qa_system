#!/usr/bin/env bash
# 中间件连通性自检（P1-5a；p1.5c 起含 Chroma）
#
# 检查什么：容器是否健康、MySQL 账号/字符集/时区是否正确、Redis 容量策略是否生效、
#          Chroma server 的 heartbeat 是否可连，
#          以及宿主机能否从 127.0.0.1 连上这三个端口（应用裸跑时走的就是这条路径）。
#
# 用法：
#   bash scripts/infra-check.sh                      # docker 已在 PATH 时
#   bash scripts/infra-check.sh /usr/local/bin/docker  # 窄 PATH 环境（如沙箱）
# 退出码：0 = 全部通过；1 = 有失败项（脚本会打印失败原因）
#
# ⚠️ 编码注意：本文件里的变量**一律写成 ${VAR} 花括号形式**。
#    原因是踩过一次：`$DB_NAME：` 这种「变量后紧跟全角标点」的写法，
#    bash 会把全角字符的首字节当成变量名的一部分，在 set -u 下报
#    「DB_NAME?: unbound variable」—— 看起来像是变量没定义，实际是解析问题。
#    中文注释/中文输出多的脚本里，花括号是唯一稳妥的写法。

set -uo pipefail

DOCKER="${1:-docker}"
cd "$(dirname "$0")/.." || exit 1

PASS=0
FAIL=0
ok()    { printf '  [ok]   %s\n' "${1}"; PASS=$((PASS + 1)); }
bad()   { printf '  [FAIL] %s\n' "${1}"; FAIL=$((FAIL + 1)); }
title() { printf '\n== %s ==\n' "${1}"; }

# 只取需要的键。不用 `source .env`：里面有 JSON 数组值（ALLOWED_ORIGINS=[...]），
# 直接 source 会被 shell 当通配符展开，报错甚至误执行。
env_val() { grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }

if [ ! -f .env ]; then
  echo "找不到 .env，请先 cp .env.example .env 并填写密码"
  exit 1
fi

DB_HOST=$(env_val MYSQL_HOST); DB_HOST="${DB_HOST:-localhost}"
DB_PORT=$(env_val MYSQL_PORT); DB_PORT="${DB_PORT:-3306}"
DB_NAME=$(env_val MYSQL_DATABASE); DB_NAME="${DB_NAME:-rag_qa}"
DB_USER=$(env_val MYSQL_USER); DB_USER="${DB_USER:-rag}"
DB_PASS=$(env_val MYSQL_PASSWORD)
RHOST=$(env_val REDIS_HOST); RHOST="${RHOST:-localhost}"
RPORT=$(env_val REDIS_PORT); RPORT="${RPORT:-6379}"
CHOST=$(env_val CHROMA_HOST); CHOST="${CHOST:-127.0.0.1}"
CPORT=$(env_val CHROMA_PORT); CPORT="${CPORT:-8001}"

# --------------------------------------------------------------------------- #
title "1. 容器状态"
# --------------------------------------------------------------------------- #
PS_OUT=$("${DOCKER}" compose ps --format '{{.Service}}|{{.State}}|{{.Status}}' 2>&1)
if [ -z "${PS_OUT}" ]; then
  bad "compose 没有返回任何服务，先跑 make infra"
else
  echo "${PS_OUT}" | sed 's/^/  /'
  for svc in mysql redis chroma; do
    line=$(echo "${PS_OUT}" | grep "^${svc}|" || true)
    case "${line}" in
      *"|running|"*"healthy"*) ok "${svc} 运行中且健康检查通过" ;;
      *"|running|"*)           bad "${svc} 在跑但健康检查未通过（可能还在初始化，看 make infra-logs）" ;;
      "")                      bad "${svc} 未创建" ;;
      *)                       bad "${svc} 状态异常 = ${line}" ;;
    esac
  done
fi

# --------------------------------------------------------------------------- #
title "2. MySQL"
# --------------------------------------------------------------------------- #
# 用应用账号连（不是 root）—— 要验的正是「应用能不能连」。
# 密码必须用 `exec -e` 传进容器：写在 docker CLI 前面的 `MYSQL_PWD=xxx` 只会留在宿主机
# 进程的环境里，容器内的 mysql 客户端看不到（症状是 "using password: NO" 的 Access denied）。
# 也不用 `-p` 参数：mysql 会往 stderr 打「命令行传密码不安全」的警告，污染下面的 DDL 探针判断。
MYSQL_Q=$("${DOCKER}" compose exec -T -e MYSQL_PWD="${DB_PASS}" mysql \
  mysql -h 127.0.0.1 -u"${DB_USER}" -D"${DB_NAME}" -N -B \
  -e "SELECT VERSION(), @@character_set_server, @@collation_server, @@time_zone;" 2>&1)
MYSQL_RC=$?

if [ "${MYSQL_RC}" -ne 0 ] || [ -z "${MYSQL_Q}" ]; then
  bad "应用账号 ${DB_USER} 连不上库 ${DB_NAME} -> $(echo "${MYSQL_Q}" | head -2 | tr '\n' ' ')"
else
  ver=$(echo "${MYSQL_Q}" | awk '{print $1}')
  cset=$(echo "${MYSQL_Q}" | awk '{print $2}')
  ccol=$(echo "${MYSQL_Q}" | awk '{print $3}')
  tz=$(echo "${MYSQL_Q}" | awk '{print $4}')
  ok "账号 ${DB_USER} 可连库 ${DB_NAME}（MySQL ${ver}）"

  # 字符集必须是 utf8mb4：中文正文之外，用户输入 emoji 时 utf8mb3 直接写入失败
  if [ "${cset}" = "utf8mb4" ]; then ok "字符集 = ${cset}"; else bad "字符集 = ${cset}，期望 utf8mb4"; fi
  if [ "${ccol}" = "utf8mb4_0900_ai_ci" ]; then ok "排序规则 = ${ccol}"; else bad "排序规则 = ${ccol}，期望 utf8mb4_0900_ai_ci"; fi
  # 时区不统一 -> 消息时间会整体偏移 8 小时，且很难查
  case "${tz}" in
    "+08:00"|"CST") ok "时区 = ${tz}" ;;
    *)              bad "时区 = ${tz}，期望 +08:00" ;;
  esac

  # 建表权限：业务表由 Alembic 迁移创建，账号没有 DDL 权限的话 P0-1 会直接卡住
  DDL_PROBE=$("${DOCKER}" compose exec -T -e MYSQL_PWD="${DB_PASS}" mysql \
    mysql -h 127.0.0.1 -u"${DB_USER}" -D"${DB_NAME}" -N -B \
    -e "CREATE TABLE IF NOT EXISTS _perm_probe (id INT PRIMARY KEY); DROP TABLE _perm_probe;" 2>&1)
  if [ -z "${DDL_PROBE}" ]; then
    ok "建表/删表权限正常（Alembic 迁移需要）"
  else
    bad "DDL 权限不足 -> $(echo "${DDL_PROBE}" | head -1)"
  fi
fi

# --------------------------------------------------------------------------- #
title "3. Redis"
# --------------------------------------------------------------------------- #
PONG=$("${DOCKER}" compose exec -T redis redis-cli ping 2>&1 | tr -d '\r')
if [ "${PONG}" = "PONG" ]; then ok "PING -> PONG"; else bad "PING 无响应 -> ${PONG}"; fi

MAXMEM=$("${DOCKER}" compose exec -T redis redis-cli config get maxmemory 2>&1 | tail -1 | tr -d '\r')
POLICY=$("${DOCKER}" compose exec -T redis redis-cli config get maxmemory-policy 2>&1 | tail -1 | tr -d '\r')
AOF=$("${DOCKER}" compose exec -T redis redis-cli config get appendonly 2>&1 | tail -1 | tr -d '\r')
# 128MB = 134217728 字节
if [ "${MAXMEM}" = "134217728" ]; then ok "maxmemory = 128MB"; else bad "maxmemory = ${MAXMEM}，期望 134217728"; fi
# noeviction 是刻意的：写满时报错，而不是静默驱逐（见迭代文档 §3.3）
if [ "${POLICY}" = "noeviction" ]; then ok "maxmemory-policy = noeviction"; else bad "maxmemory-policy = ${POLICY}，期望 noeviction"; fi
if [ "${AOF}" = "yes" ]; then ok "appendonly = yes"; else bad "appendonly = ${AOF}，期望 yes"; fi

# 队列用 db1、缓存用 db0，两个库都要能选到
DB1=$("${DOCKER}" compose exec -T redis redis-cli -n 1 dbsize 2>&1 | tr -d '\r' | head -1)
case "${DB1}" in
  ''|*[!0-9]*) bad "db1 不可用 -> ${DB1}" ;;
  *)           ok "db1 可访问（队列 broker 用这个库，当前 ${DB1} 个 key）" ;;
esac

# --------------------------------------------------------------------------- #
title "4. Chroma（server 模式，p1.5c 起）"
# --------------------------------------------------------------------------- #
# heartbeat 是 chroma 的就绪探针：能应答说明 HTTP API 可用。
# 宿主机映射端口（默认 8001）就是裸跑的 backend/worker 实际连的地址。
# 0.5.23 稳定的是 /api/v1/heartbeat（v2 在部分 0.5.x 镜像上没有）
HB=$(python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://${CHOST}:${CPORT}/api/v1/heartbeat', timeout=3)
    print(r.status)
except Exception:
    sys.exit(1)
" 2>/dev/null)
if [ "${HB}" = "200" ]; then
  ok "heartbeat 可连（http://${CHOST}:${CPORT}/api/v1/heartbeat）"
else
  bad "heartbeat 连不上 http://${CHOST}:${CPORT}（先 make infra 起 chroma）"
fi

# --------------------------------------------------------------------------- #
title "5. 宿主机端口可达性（应用裸跑走这条路径）"
# --------------------------------------------------------------------------- #
check_port() {
  local name="${1}" host="${2}" port="${3}"
  if python3 -c "
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect(('${host}', ${port})); s.close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    ok "${name} ${host}:${port} 可连"
  else
    bad "${name} ${host}:${port} 连不上"
  fi
}
check_port MySQL "${DB_HOST}" "${DB_PORT}"
check_port Redis "${RHOST}" "${RPORT}"
check_port Chroma "${CHOST}" "${CPORT}"

# --------------------------------------------------------------------------- #
echo
if [ "${FAIL}" -eq 0 ]; then
  echo "全部通过（${PASS} 项）"
  exit 0
else
  echo "通过 ${PASS} 项，失败 ${FAIL} 项"
  exit 1
fi
