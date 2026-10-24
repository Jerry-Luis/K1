#!/usr/bin/env bash
# ============================================================================
# 恢复 home 目录态配置（.trade_system_remote / .git-credentials 不进仓库）
#
# 背景：沙箱重置会清空 $HOME，导致 bootstrap 找不到远端地址、
#       git 无法认证推送。本脚本从仓库内 .remote 重建这两项。
#
# 凭据来源优先级：
#   1. 环境变量 GITHUB_TOKEN（推荐，配合定时任务的 secret 注入）
#   2. 已存在的 ~/.git-credentials（本机首次运行场景）
#   3. 都没有 -> 只能匿名拉取（公开仓库可用，私有仓库会失败）
# ============================================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'

# --- 1. 远端地址
if [ ! -s "$HOME/.trade_system_remote" ] && [ -s "$BASE/.remote" ]; then
  grep -v '^#' "$BASE/.remote" | head -1 > "$HOME/.trade_system_remote"
  printf '%s✓%s 已从仓库恢复远端地址: %s\n' "$GRN" "$RST" "$(cat "$HOME/.trade_system_remote")"
fi

# --- 2. git 凭据
git config --global credential.helper store 2>/dev/null || true

if [ -n "${GITHUB_TOKEN:-}" ]; then
  printf 'https://%s:%s@github.com\n' "${GITHUB_USER:-Jerry-Luis}" "$GITHUB_TOKEN" > "$HOME/.git-credentials"
  chmod 600 "$HOME/.git-credentials"
  printf '%s✓%s 已用 GITHUB_TOKEN 写入凭据（用户 %s）\n' "$GRN" "$RST" "${GITHUB_USER:-Jerry-Luis}"
elif [ -s "$HOME/.git-credentials" ]; then
  chmod 600 "$HOME/.git-credentials"
  printf '%s✓%s 凭据已存在\n' "$GRN" "$RST"
else
  # 无凭据时关闭交互提示，避免非交互执行时挂起；公开仓库仍可匿名读
  export GIT_TERMINAL_PROMPT=0
  printf '%s!%s 无推送凭据：仅可匿名读取公开仓库，无法推送\n' "$YEL" "$RST"
  printf '%s  修复：在定时任务配置中注入 GITHUB_TOKEN（repo 权限）%s\n' "$YEL" "$RST"
fi

# --- 3. git 提交身份（同样存在 $HOME/.gitconfig，重置后会丢）
if ! git config --global user.email >/dev/null 2>&1; then
  git config --global user.email "${GIT_AUTHOR_EMAIL:-trade@local}"
  git config --global user.name  "${GIT_AUTHOR_NAME:-trade-system}"
  printf '%s✓%s 已恢复 git 提交身份\n' "$GRN" "$RST"
fi
