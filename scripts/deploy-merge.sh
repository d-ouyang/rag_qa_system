#!/usr/bin/env bash
# 一键部署合并：develop → main 并推送（push origin main = 触发线上自动部署）
#
# 用法：
#     bash scripts/deploy-merge.sh        # 或 make deploy
#
# 设计原则：**要么干净地合上并推送，要么什么都不留下**。
# 合并冲突 / 任何失败 → 自动 abort 回到合并前状态、不推送、不触发部署，
# 并回到你出发时所在的分支 —— 不存在「合到一半卡住等你处理」的中间态。

set -euo pipefail

BRANCH_FROM="develop"
BRANCH_TO="main"

# ── 0. 前置检查 ──────────────────────────────────────────────────────────
current="$(git branch --show-current)"
if [ -z "$current" ]; then
  echo "✗ 当前处于 detached HEAD，先切回一个分支再执行"; exit 1
fi

# 工作区必须干净（未提交改动会阻塞 checkout 或混进合并）。
# --untracked-files=no：.workbuddy/ 之类的未追踪文件不影响合并，不算阻碍。
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "✗ 工作区有未提交改动，先 commit 或 stash："
  git status --short --untracked-files=no
  exit 1
fi

git fetch origin --quiet

# develop 必须先推送：main 合并的是本地 develop，若它领先于远端，
# 会出现「线上 main 有、origin/develop 没有」的脱节，下次部署对不上账
if [ "$(git rev-parse "$BRANCH_FROM")" != "$(git rev-parse "origin/$BRANCH_FROM")" ]; then
  echo "✗ 本地 develop 与 origin/develop 不一致，先执行：git push origin develop"
  git log --oneline "origin/$BRANCH_FROM..$BRANCH_FROM" | head -5
  exit 1
fi

# main 上不允许有 develop 之外的**真实改动**（main 只靠合并前进，这是本仓库的约定）。
# ⚠️ 只查非合并提交：每次部署都会在 main 留一个 develop 里没有的合并节点，
#    那是流程的正常产物；但合并节点的内容必须全部来自其父提交（即 develop），
#    所以「main 比 develop 多出的非合并提交」为空 = main 没有私货。
#    用 `git log --no-merges main --not develop` 检出：有输出 = 有人绕过流程
#    直接改了 main，在这里拦住比合并出「两边各走各的」强得多。
if [ -n "$(git log --no-merges --oneline "origin/$BRANCH_TO" --not "$BRANCH_FROM")" ]; then
  echo "✗ origin/main 上有 develop 不包含的**真实改动**（非合并提交），先处理："
  git log --no-merges --oneline "origin/$BRANCH_TO" --not "$BRANCH_FROM" | head -5
  echo "  把这些变更带回 develop 后再部署；或确认可丢弃后强推 main（后者需要人工决定）"
  exit 1
fi

# ── 1. 合并 ──────────────────────────────────────────────────────────────
git checkout "$BRANCH_TO" --quiet
# 本地 main 对齐远端（--ff-only：main 若有本地独有提交会失败，不会吞掉任何东西）
git pull --ff-only origin "$BRANCH_TO" --quiet

echo "── 合并 $BRANCH_FROM → $BRANCH_TO ──"
# --no-edit：接受默认合并信息，不弹编辑器（git merge「卡住」几乎都是这个原因）
if ! git merge --no-ff --no-edit "$BRANCH_FROM"; then
  echo "✗ 合并失败（冲突或其它错误），已自动回滚：未推送、未触发部署"
  git merge --abort 2>/dev/null || true
  git reset --hard --quiet          # 清掉可能残留的冲突标记
  git checkout "$current" --quiet
  echo "  已回到分支 $current，工作区与合并前一致。解决冲突后在 develop 上处理。"
  exit 1
fi

# ── 2. 推送（部署触发点）────────────────────────────────────────────────
if ! git push origin "$BRANCH_TO"; then
  echo "✗ 推送失败（网络/权限）。本地 main 已完成合并但未推送，"
  echo "  网络恢复后重跑本脚本或手动 git push origin main 即可。"
  git checkout "$current" --quiet
  exit 1
fi

git checkout "$current" --quiet
echo "✓ main 已推送（$(git log --oneline -1 origin/$BRANCH_TO | head -c 60)…），线上部署已触发"
