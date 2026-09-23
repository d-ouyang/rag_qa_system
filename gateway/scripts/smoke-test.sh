#!/usr/bin/env bash
#
# 网关端到端冒烟测试 —— 一条命令验证「鉴权 + 限流 + 转发 + 流式」是否都还正常。
#
# 为什么做成脚本而不是单元测试：
# 网关的全部价值在于「请求能不能正确地走过去」，这件事只有在**真实链路**上
# 才验证得了（假的后端只能验证守卫，验证不了 fixRequestBody / 代理超时 /
# NDJSON 透传这些真正容易坏的地方）。所以这里用 curl 打真实接口。
#
# 用法：
#   ./scripts/smoke-test.sh                       # 默认打 http://127.0.0.1:3000
#   ./scripts/smoke-test.sh http://gw.example.com # 打指定网关
#   SMOKE_USER=admin SMOKE_PASS=admin123 ./scripts/smoke-test.sh
#
# 前置：网关与后端都已启动（先 curl /api/health 确认）。
# 退出码：0 全部通过；1 有失败项（便于接进 CI）。
#
# 注意：脚本里所有 curl 都带 --noproxy '*'。
# 开发机上常挂着 HTTP_PROXY（科学上网），而代理不认 127.0.0.1，
# 会返回一个来源不明的 502，让人误以为服务挂了 —— 这个坑踩过一次。

set -uo pipefail

BASE="${1:-http://127.0.0.1:3000}"
USER_NAME="${SMOKE_USER:-admin}"
USER_PASS="${SMOKE_PASS:-admin123}"
CURL=(curl -s --noproxy '*' -m 30)

PASS=0
FAIL=0
TOKEN=""

green() { printf '\033[32m%s\033[0m' "$1"; }
red() { printf '\033[31m%s\033[0m' "$1"; }

# check <用例名> <期望> <实际>
check() {
  local name="$1" expect="$2" actual="$3"
  if [ "$expect" = "$actual" ]; then
    PASS=$((PASS + 1)); printf '  [%s] %s\n' "$(green PASS)" "$name"
  else
    FAIL=$((FAIL + 1)); printf '  [%s] %s | 期望 %s 实际 %s\n' "$(red FAIL)" "$name" "$expect" "$actual"
  fi
}

section() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# --------------------------------------------------------------------------- #
section "0. 网关自身健康检查（公开路由，不需要 token）"
code=$("${CURL[@]}" -o /tmp/smoke_health -w '%{http_code}' "$BASE/api/health")
check "GET /api/health 返回 200" "200" "$code"
check "健康体里带 service 标识" "rag-qa-gateway" \
  "$(grep -o '"service":"[^"]*"' /tmp/smoke_health | head -1 | cut -d'"' -f4)"

# --------------------------------------------------------------------------- #
section "0b. 上游健康检查白名单（PLAN-v2.0.0 P0-2：免 token 放行）"
# 这两条盯的是 proxy.controller.ts 里的**声明顺序**：具体路径必须先于 `api/v1/*`
# 注册，否则会回落到通配路由被守卫拒成 401，白名单静默失效。
for path in api/v1/system/health api/v1/qa/health; do
  code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/$path")
  check "GET /$path 免 token 可访问" "200" "$code"
done

# 反向断言：白名单必须是**精确路径**。如果哪天有人图省事改成前缀通配，
# 这里会立刻变红——那意味着 /api/v1/system/* 下的接口全裸奔了。
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/v1/system/memory")
check "同前缀的 /api/v1/system/memory 仍需 token（白名单没被放大）" "401" "$code"

# --------------------------------------------------------------------------- #
section "1. 未带 token 访问受保护接口（应 401 且错误体统一）"
code=$("${CURL[@]}" -o /tmp/smoke_401 -w '%{http_code}' "$BASE/api/v1/qa/sessions")
check "无 token 返回 401" "401" "$code"
check "错误体含 error.code" "TOKEN_MISSING" \
  "$(grep -o '"code":"[^"]*"' /tmp/smoke_401 | head -1 | cut -d'"' -f4)"
check "错误体含 requestId" "yes" \
  "$(grep -q '"requestId":"' /tmp/smoke_401 && echo yes || echo no)"

code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer forged.token.value' "$BASE/api/v1/qa/sessions")
check "伪造 token 返回 401" "401" "$code"

# --------------------------------------------------------------------------- #
section "2. 登录接口"
code=$("${CURL[@]}" -o /tmp/smoke_bad -w '%{http_code}' -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"definitely-wrong\"}")
check "错误密码返回 401" "401" "$code"
check "错误码为 INVALID_CREDENTIALS" "INVALID_CREDENTIALS" \
  "$(grep -o '"code":"[^"]*"' /tmp/smoke_bad | head -1 | cut -d'"' -f4)"

code=$("${CURL[@]}" -o /tmp/smoke_nouser -w '%{http_code}' -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' -d '{"username":"no-such-user-xyz","password":"whatever"}')
check "用户名不存在也是 401（不泄露账号是否存在）" "401" "$code"

code=$("${CURL[@]}" -o /tmp/smoke_login -w '%{http_code}' -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"$USER_PASS\"}")
check "正确凭据返回 200" "200" "$code"
TOKEN=$(sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p' /tmp/smoke_login)
check "拿到 access_token" "yes" "$([ -n "$TOKEN" ] && echo yes || echo no)"

if [ -z "$TOKEN" ]; then
  printf '\n%s 没拿到 token，后续用例无法执行。请确认 GATEWAY_USERS 里的密码与 SMOKE_PASS 一致。\n' "$(red '中止：')"
  exit 1
fi

code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/auth/me")
check "GET /api/auth/me 带合法 token 返回 200" "200" "$code"

# --------------------------------------------------------------------------- #
section "3. 代理转发（带 token 打后端）"
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/qa/sessions")
check "GET /api/v1/qa/sessions 返回 200" "200" "$code"

code=$("${CURL[@]}" -o /tmp/smoke_mem -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/system/memory")
check "GET /api/v1/system/memory 返回 200" "200" "$code"
check "记忆接口报出存储后端" "yes" \
  "$(grep -q '"backend":"' /tmp/smoke_mem && echo yes || echo no)"

# POST 带 body：验证 body 没被 body-parser 吃掉（fixRequestBody 是否生效）
code=$("${CURL[@]}" -o /tmp/smoke_ask -w '%{http_code}' -X POST "$BASE/api/v1/qa/ask" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"报销流程是什么？"}')
check "POST /api/v1/qa/ask 返回 200（body 转发正常）" "200" "$code"
check "响应含 session_id（说明请求真的被后端处理了）" "yes" \
  "$(grep -q '"session_id"' /tmp/smoke_ask && echo yes || echo no)"

# --------------------------------------------------------------------------- #
section "4. 流式问答（NDJSON 逐帧透传）"
"${CURL[@]}" -N -X POST "$BASE/api/v1/qa/ask/stream" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"你好"}' > /tmp/smoke_stream 2>/dev/null
check "流式有 session 首帧" "yes" "$(grep -q '"type": "session"' /tmp/smoke_stream && echo yes || echo no)"
check "流式有 chunk 增量帧" "yes" "$(grep -q '"type": "chunk"' /tmp/smoke_stream && echo yes || echo no)"
check "流式有 done 结束帧" "yes" "$(grep -q '"type": "done"' /tmp/smoke_stream && echo yes || echo no)"

code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/qa/ask/stream" \
  -H 'Content-Type: application/json' -d '{"question":"hi"}')
check "无 token 的流式请求被挡在 401（不是 200 带错误帧）" "401" "$code"

# --------------------------------------------------------------------------- #
section "5. 登录限流（防暴力破解）"
throttled=0
for _ in $(seq 1 10); do
  c=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/login" \
    -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"bad\"}")
  [ "$c" = "429" ] && throttled=$((throttled + 1))
done
check "连续错误登录会触发 429 限流" "yes" "$([ "$throttled" -gt 0 ] && echo yes || echo no)"

# --------------------------------------------------------------------------- #
printf '\n==================================================\n'
if [ "$FAIL" -eq 0 ]; then
  printf '结果：%s 通过 / %s 失败\n' "$(green "$PASS")" "$FAIL"
  exit 0
fi
printf '结果：%s 通过 / %s 失败\n' "$PASS" "$(red "$FAIL")"
exit 1
