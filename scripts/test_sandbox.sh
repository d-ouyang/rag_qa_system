#!/usr/bin/env bash
# 在**一次性数据库 + 独立collection** 上跑回归，真知识库完全不受影响。
#
# --------------------------------------------------------------------------
# 为什么需要（2026-10-07 事故的直接产物）
# --------------------------------------------------------------------------
# `make test` 里某个测试脚本曾写了 `sa_delete(document_table)`（**无 where 的全表
# 删除**），跑一轮就把本机 35 篇真实知识库文档的登记行清掉了，而它们的 90 个
# 切片还在 Chroma 里 —— 于是前端出现「文档数 0 / 片段总数 90」这种自相矛盾的
# 现象（两个数字来自两个数据源），问答则一律回「根据现有资料无法回答」。
# 原文件在 `upload/` 里没丢，`scripts/reindex.py` 一次补回（实测 35/35），
# 但**不该由一次 make test 触发**。
#
# `make test` 现在挂了护栏（见 Makefile 的 `test:` 目标），真库有数据就**拒绝执行**。
# 但「拒绝」只是止损，真正的解法是**给回归一个不碰真数据的地方跑** —— 也就是这里。
#
# --------------------------------------------------------------------------
# 隔离方式（⚠️ 变量名全部核对过 `config/settings.py` 与 `.env`，别凭直觉写）
# --------------------------------------------------------------------------
#   MySQL   → 新库 `rag_qa_sandbox`，跑完删        `MYSQL_DATABASE`
#   Chroma  → **同一个 server 上换一个 collection 名**，跑完删
#              ⚠️ 不是换目录！本机 Chroma 是 **server 模式**
#              （`.env` 里 `CHROMA_HOST=127.0.0.1` / `CHROMA_PORT=8001`），
#              索引在**容器里**（`chroma_data/` 只是本地那几份历史遗留，
#              真索引不在那里）。换目录这种写法在这里**完全是空操作**。
#              真要换目录得把 `CHROMA_HOST` 置空退回嵌入式，那是另一套运行模式，
#              代价大得多，而且和生产形态不一致 —— 那种测试结果没有参考价值。
#              collection 名：`core/vector_store.py`的 `DEFAULT_COLLECTION_NAME`
#              = `rag_qa_knowledge`，测试用 `rag_qa_knowledge_sandbox`。
#              ⚠️ 变量名 `DEFAULT_COLLECTION_NAME` **不是环境变量**
#              （它是 class 属性，不走 pydantic settings），所以只能由测试脚本
#              自己传 collection_name 进去 —— 这也是为什么沙箱模式
#              **不能覆盖全部测试**（见下面「已知局限」）。
#   Redis   → 换db（`.env` 里 `REDIS_URL` 是 redis://localhost:6379/0）
#   upload/ → 换临时目录                `UPLOAD_DIR`
#
# --------------------------------------------------------------------------
# 已知局限（别以为它是万能的）
# --------------------------------------------------------------------------
# ① **collection 名不是环境变量**，所以除非测试脚本自己传，
#    沙箱模式**隔离不了 Chroma**。要完全隔离，得给测试脚本加参数 ——
#    那是独立的一笔改动，不该塞进这次止损。
#    → 现阶段沙箱模式的**真实价值是隔离 MySQL**（也就是清空 `document` 表
#      那一类破坏），Chroma 侧仍需靠护栏的静态检查兜。
# ② 沙箱库是**新库**，没有种子数据。部分测试若断言「表里有种子数据」，
#    在沙箱下可能不成立 —— **失败不等于代码坏了**。
# ③ 慢：建库 + 全量迁移约 20~40 秒。
# ④ **退出期原生崩溃**（2026-10-16 实测，约 1/3 概率）：`test_module4_llm_and_retriever`
#    会在**所有断言跑完之后**、解释器退出阶段崩在原生库：
#        libc++abi: terminating ... recursive_mutex lock failed: Invalid argument
#    死相**有两种**（同根不同相）：SIGABRT（exit 134）与 SIGSEGV（exit 139）。
#    此时断言是 87 通过 / 0 失败 —— **不是代码坏了**，是 macOS 上
#    torch/MPS + ollama HTTP 客户端在 finalization 时的遗留线程问题。
#    实测多次：134/0/0、139（P4-5b 期间又见），断言均 87/0（改动前同样会中，非回归）。
#    → 判据：退出码 **≥128（被信号杀死）** 且 输出含「汇总：通过 N 项，失败 0 项」
#      → 计入已知 flake，不算失败（见下面 `_run_test`）；中途真崩（没打出汇总行）
#        仍照常算失败。首版只摘 134，SIGSEGV 当场漏网 —— 教训记坑 98。
#
# --------------------------------------------------------------------------
# 用法
# --------------------------------------------------------------------------
#     make test-sandbox# 一次性库上跑全套
#     make test-sandbox s=9        # 只跑 module9（验证它不再清真库）
#     make kb-guard-count          # 只看真库当前有多少篇文档
#     make kb-restore              # 误清之后补登记（scripts/reindex.py --apply）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
SANDBOX_DB="rag_qa_sandbox"

# ⚠️ 沙箱用的是**真库的连接信息**（host/port/user/pwd照旧），
#    只换 MYSQL_DATABASE。所以下面这些从 .env 读，不是硬编码。
DB_HOST="$(grep -E '^MYSQL_HOST=' .env | head -1 | cut -d= -f2-)"
DB_PORT="$(grep -E '^MYSQL_PORT=' .env | head -1 | cut -d= -f2-)"
DB_USER="$(grep -E '^MYSQL_USER=' .env | head -1 | cut -d= -f2-)"
DB_PASS="$(grep -E '^MYSQL_PASSWORD=' .env | head -1 | cut -d= -f2-)"
DB_ROOT="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2-)"
# --------------------------------------------------------------------------
# upload/ 的隔离方式：**不能用 /tmp**（踩过）
# --------------------------------------------------------------------------
# 第一版把 `UPLOAD_DIR` 指到 `mktemp -d`（/tmp/...），结果 module9 直接崩：
#     ParseError: 文件不存在：m9_xxx_原始名.txt
# 根因是 `core/parsing.py` 的 `resolve_storage_path()`：库里存的 `storage_path`
# 是**相对于项目根**的（约定见 alembic/0001 的注释：绝对路径进库会让本地开发
# 与容器互相读不到），所以它无条件按 `settings.BASE_DIR / path` 解析。
# upload 挪到 /tmp 之后，「项目根/upload/xxx」就不存在了。
#
# 那条约定是**生产语义**，不能为了测试去改它（改了就等于承认库里可以存绝对路径）。
# 所以正确做法是：**让沙箱目录仍留在项目内**，用环境变量指过去 ——
#   `UPLOAD_DIR=<项目根>/upload_sandbox`（跑完删）。
# 这样「项目根 / upload_sandbox / xxx」这条相对路径依然成立，
# 生产代码一行都不用动。
SANDBOX_UPLOAD="$ROOT/upload_sandbox"
UPLOAD_DIR="$SANDBOX_UPLOAD"

# 🔴 **删目录前的硬断言**。上面这行 `rm -rf` 会真的删东西，所以必须先证明
# 它要删的**不是**真知识库目录。
# 这不是洁癖：本脚本第一版把 UPLOAD_DIR 指到 mktemp -d，若哪次变量没赋值成功，
# `rm -rf ""` 侥幸无害，但 `rm -rf "$UPLOAD_DIR"` 在变量为空时会变成 `rm -rf ""`
# —— 或者是 `rm -rf "/"`。用真实路径做「不等且不为空」断言，才拦得住这类事故。
# 与个人文件安全的原则一致：**破坏性操作前先列出将影响什么**。
if [ "$SANDBOX_UPLOAD" = "$ROOT/upload" ] || [ -z "$SANDBOX_UPLOAD" ] \
   || [ "$SANDBOX_UPLOAD" = "$ROOT" ] || [ "$SANDBOX_UPLOAD" = "/" ]; then
  echo "✗ 拒绝执行：沙箱 upload 目录指向了危险路径（${SANDBOX_UPLOAD}）" >&2
  exit 1
fi

cleanup() {
  local code=$?
  echo
  echo "── 清理沙箱 ──────────────────────────────────────"
  if [ "$code" -eq 0 ]; then
    echo "  ✓ 回归全绿"
  else
    echo "  ⚠️ 回归退出码 $code"
    echo "     新库无种子数据，部分断言可能天然不成立 —— 先看是不是这个原因"
  fi
  rm -rf "$SANDBOX_UPLOAD"
  echo "  ✓ 已删除沙箱upload 目录 ${SANDBOX_UPLOAD}"
  "$PY" - "$DB_HOST" "$DB_PORT" "$DB_ROOT" "$SANDBOX_DB" <<'PYEOF' 2>/dev/null \
    && echo "  ✓ 已删除沙箱库 ${SANDBOX_DB}" \
    || echo "  ⚠️ 沙箱库删除失败，手动：mysql -h${DB_HOST} -P${DB_PORT} -u root -p -e 'DROP DATABASE \`${SANDBOX_DB}\`'"
import sys
import pymysql
host, port, root_pwd, db = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
c = pymysql.connect(host=host, port=port, user="root", password=root_pwd, autocommit=True)
with c.cursor() as cur:
    cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
PYEOF
  echo "  ✓ 真知识库（rag_qa 库 / upload/）未触碰"
  echo "  ⚠️ Chroma 未隔离（collection 名不是环境变量，见脚本头部说明）"
  exit "$code"
}
trap cleanup EXIT

echo "═══ 沙箱回归 ═══"
echo "  MySQL:   ${DB_HOST}:${DB_PORT} / ${SANDBOX_DB}（一次性）"
echo "  upload/: ${UPLOAD_DIR}（临时）"
echo "  Chroma:  ⚠️ 未隔离 —— collection 名不是环境变量"
echo

# ---- 建库 ----
# ⚠️ 必须**显式授权**：MySQL 的权限是「库级」的，不会因为能连`rag_qa`
#    就自动覆盖`rag_qa_sandbox`。少了 GRANT，迁移会报
#    `1044 Access denied for user 'rag'@'%' to database 'rag_qa_sandbox'`。
#    用户名/密码从 .env 读而不是硬编码 —— 硬编码等于让脚本在换机器后静默失效。
"$PY" - "$DB_HOST" "$DB_PORT" "$DB_ROOT" "$SANDBOX_DB" "$DB_USER" <<'PYEOF'
import sys
import pymysql
host, port, root_pwd, db, db_user = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
c = pymysql.connect(host=host, port=port, user="root", password=root_pwd, autocommit=True)
with c.cursor() as cur:
    cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
    cur.execute(f"CREATE DATABASE `{db}` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci")
    # 只授「这一库」的权限，不图省事给 *.*：
    # 沙箱脚本一旦有bug，也不至于把生产库一起带走。
    cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{db_user}'@'%'")
    cur.execute("FLUSH PRIVILEGES")
print(f"✓ 沙箱库 {db} 已建并授权给 {db_user}")
PYEOF

# ---- 导出环境：只改库名与upload 目录，其余（host/port/账号/嵌入模型）照旧 ----
export MYSQL_DATABASE="$SANDBOX_DB"
export UPLOAD_DIR
export REDIS_URL="$(grep -E '^REDIS_URL=' .env | head -1 | cut -d= -f2- | sed 's#/0$#/15#')"
export EMBEDDING_BACKEND=local RERANK_BACKEND=local
# 🔴 回归固定关混合检索（P3-4）：「关 = 与 P3-4 之前逐字节一致」是必须守住的不变量，
#    不能跟着开发者本机 .env 的开关值漂。module20 自己在进程内翻开关做混合态测试。
export RETRIEVAL_HYBRID=false
# 🔴 回归固定关 MinerU（P3-7b）：回归不允许真调外部 API（额度/网络/时长都不可控），
#    MinerU 路径由 test_p37b_mineru.py 的 mock 层覆盖；这里的 PDF 测试统一走 PyMuPDF。
export MINERU_ENABLED=false

echo "  MYSQL_DATABASE=${MYSQL_DATABASE}"
echo "  REDIS_URL=${REDIS_URL}     （换到db 15，不碰真缓存）"
echo "  UPLOAD_DIR=${UPLOAD_DIR}"
echo

# ---- 跑迁移 ----
echo "── 跑迁移 ────────────────────────────────────────"
"$PY" -m alembic upgrade head 2>&1 | tail -3
echo

# ---- 跑测试 ----
# 跑单个测试脚本，并把「退出期原生崩溃」从「真失败」里摘出来（已知局限 ④）。
# 判据必须**同时**满足：退出码 ≥128（被信号杀死，SIGABRT/6=134、SIGSEGV/11=139
# 都实测出现过——同一 flake 的两种死相）**且**输出里有「…失败 0 项」的汇总行。
# 只满足一半（真崩、没跑到汇总）仍然算失败 —— 不放过真问题。
# 非 128+ 的退出码（断言失败 exit 1 等）一律照常算失败。
TEARDOWN_ABORT=""
_run_test() {
  local script="$1" logf rc
  logf="$(mktemp)"
  if "$PY" "$script" 2>&1 | tee "$logf"; then
    rc=0
  else
    rc=${PIPESTATUS[0]}
  fi
  if [ "$rc" -ge 128 ] \
     && grep -qE "汇总：通过 [0-9]+ 项，失败 0 项|通过 / 0 失败" "$logf"; then
    echo "⚠️  $(basename "$script") 退出期原生崩溃（信号杀死，exit=${rc}），但断言全过"
    echo "    → 已知环境 flake，见本脚本头部「已知局限」④，不计入失败"
    TEARDOWN_ABORT="${TEARDOWN_ABORT} $(basename "$script")"
    rm -f "$logf"
    return 0
  fi
  rm -f "$logf"
  return "$rc"
}

if [ -n "${s:-}" ]; then
  echo "── 只跑 module${s} ─────────────────────────────────"
  # shellcheck disable=SC2086
  _SF=""
  for f in tests/test_module${s}_*.py; do
    echo "--- $f"
    if ! _run_test "$f"; then
      echo "❌ $f 失败"
      _SF="${_SF} ${f}"
    fi
  done
  [ -z "$_SF" ] || { echo "═══ 失败的脚本：${_SF} ═══"; exit 1; }
else
  echo "── 跑全套回归 ────────────────────────────────────"
  TESTS="
    test_module1_config
    test_module2_document_loader
    test_p37a_title_mode
    test_p37b_mineru
    test_module3_vectorstore
    test_module4_llm_and_retriever
    test_module5_rag_chain_api
    test_module6_session_store
    test_qa_cache
    test_module7_redis_over_tcp
    test_module8_mysql_session_store
    test_module9_async_pipeline
    test_module10_chunk_refs
    test_module11_account
    test_module12_admin
    test_module12_audit
    test_module13_login
    test_module15_session_isolation
    test_module18_download_gate
    test_module19_eval
    test_module20_hybrid
    test_module21_quota_snapshot
    test_module23_query_enhance
    test_module24_config_service
    test_module25_admin_config
    test_module26_qa_trace
    test_module27_qa_flow_trace
    test_module28_groundedness
    test_module29_session_summary
  "
  # 🔴 **不在第一个失败处停下**（2026-10-11 改，P3-2 的教训）：
  #    原来写的是 `|| { echo "❌ $t 失败"; exit 1; }`，于是
  #    「只有一条失败」会变成「只剩一条被执行」—— 后面的模块**一次都没跑**。
  #    实测：一条列注释漂移（module8）让 9/10/11/12/13/15 全部静默跳过三个版本，
  #    而每轮看到的都是「82/1」，很容易被当成「就那一条老问题」。
  #    现在跑完全部，最后再汇总：失败**可见**比失败**早停**重要得多。
  FAILED=""
  for t in $TESTS; do
    echo "--- $t"
    if ! _run_test "tests/${t}.py"; then
      echo "❌ ${t} 失败"
      FAILED="${FAILED} ${t}"
    fi
  done
  if [ -n "$TEARDOWN_ABORT" ]; then
    echo
    echo "── 退出期原生崩溃（断言全过，计入已知 flake）──${TEARDOWN_ABORT}"
  fi
  if [ -n "$FAILED" ]; then
    echo
    echo "═══ 失败的模块：${FAILED} ═══"
    echo "  （沙箱是新库、无种子数据：module12_audit / module13_login 一类"
    echo "   「依赖种子账号」的断言天然不成立 —— 先看是不是这个原因，见脚本头部「已知局限」）"
    exit 1
  fi
fi