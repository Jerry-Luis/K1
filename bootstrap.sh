#!/usr/bin/env bash
# ============================================================================
# 持久化引导 — 每次定时任务开始时调用
#
# 用途：解决"沙箱每次调度都是全新实例，/workspace 内容丢失"的问题。
# 若本地交易系统不完整，则从远端仓库恢复代码与配置（不含密钥、不含产物）。
#
# 配置远端（二选一，写入 ~/.trade_system_remote 或环境变量 TRADE_GIT_REMOTE）：
#   git remote add origin <url>  ; git push -u origin master
#   或在本地私有 Git 服务 / 内网 Gitea 建库后填入 URL
#
# 用法：
#   bash bootstrap.sh            # 自检 + 缺失则恢复
#   bash bootstrap.sh --force    # 强制覆盖本地
# ============================================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_FILE="$HOME/.trade_system_remote"

# 硬编码兜底：bootstrap.sh 自身就是入口，沙箱重置后唯一能保证存在的东西
# （若目录被整个删除，连这个文件都没有，属于无解场景，见 README）
DEFAULT_REMOTE="https://github.com/Jerry-Luis/K1.git"

REMOTE="${TRADE_GIT_REMOTE:-}"
[ -z "$REMOTE" ] && [ -f "$REMOTE_FILE" ] && REMOTE="$(head -1 "$REMOTE_FILE" | tr -d '[:space:]')"
[ -z "$REMOTE" ] && [ -f "$BASE/.remote" ] && REMOTE="$(grep -v '^#' "$BASE/.remote" | head -1 | tr -d '[:space:]')"
[ -z "$REMOTE" ] && REMOTE="$DEFAULT_REMOTE"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'

echo "═══ 交易系统持久化引导 ═══"

# 完整性判定：核心脚本 + 模型配置齐全才算完整
complete() {
  local need=(
    "run_screen.sh" "config/model_v3.json" "config/model_v3.md"
    "scripts/datasource.py" "scripts/model.py"
    "scripts/pipeline.py" "scripts/reporter.py"
  )
  for f in "${need[@]}"; do [ -s "$BASE/$f" ] || return 1; done
  return 0
}

if complete && [ "$FORCE" -eq 0 ]; then
  printf '%s✓%s 本地系统完整，无需恢复\n' "$GRN" "$RST"
  exit 0
fi

# 列出缺失项，便于定位
echo "缺失文件："
for f in run_screen.sh config/model_v3.json config/model_v3.md \
         scripts/datasource.py scripts/model.py scripts/pipeline.py scripts/reporter.py; do
  [ -s "$BASE/$f" ] || echo "  - $f"
done

echo "本地文件不完整或强制恢复，尝试从远端拉取…"

if [ -z "$REMOTE" ]; then
  printf '%s!%s 未配置远端仓库。\n' "$YEL" "$RST"
  cat <<'EOF'

  当前依赖本地文件，沙箱重置即丢失。请配置远端之一：

  A) 私有 Git（推荐，支持版本追溯）
     cd /workspace/交易系统
     git remote add origin <你的私有仓库URL>
     git push -u origin master
     echo "<你的私有仓库URL>" > ~/.trade_system_remote

  B) 仅靠本地磁盘（不安全）
     将 /workspace/交易系统 整体挂载为持久卷

EOF
  complete && { printf '%s!%s 本地文件可用，继续执行（存在丢失风险）\n' "$YEL" "$RST"; exit 0; }
  printf '%s✗ 文件缺失且无远端，无法恢复%s\n' "$RED" "$RST"
  exit 3
fi

echo "  远端: $REMOTE"
if [ "$FORCE" -eq 1 ]; then rm -rf "$BASE"; fi

TMP="$(mktemp -d)"
CLONE_LOG="$TMP/clone.log"

# 逐个尝试：带凭据 -> 匿名
CLONE_OK=0
if GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REMOTE" "$TMP/ts" >"$CLONE_LOG" 2>&1; then
  CLONE_OK=1
fi

if [ "$CLONE_OK" -eq 1 ]; then
  # 用 rsync 语义覆盖（存在则更新，不报错）；排除本地日志/缓存
  mkdir -p "$BASE"
  ( cd "$TMP/ts" && tar cf - --exclude=.git . 2>/dev/null ) | ( cd "$BASE" && tar xf - 2>/dev/null )
  # 单独处理 .git，保证后续可 push
  rm -rf "$BASE/.git" && mv "$TMP/ts/.git" "$BASE/.git" 2>/dev/null
  # 在恢复的仓库上重设远端，避免凭据残留在 URL 中
  git -C "$BASE" remote remove origin 2>/dev/null
  git -C "$BASE" remote add origin "$REMOTE" 2>/dev/null
  rm -rf "$TMP"
  if complete; then
    printf '%s✓%s 恢复完成（%s）\n' "$GRN" "$RST" "$(git -C "$BASE" rev-parse --short HEAD 2>/dev/null || echo 'no-git')"
    exit 0
  fi
  printf '%s✗ 恢复后文件仍不完整%s\n' "$RED" "$RST"; exit 3
else
  printf '%s✗ 从远端拉取失败：%s\n' "$RED" "$RST"
  sed -n '1,6p' "$CLONE_LOG" | sed 's/^/    /'
  rm -rf "$TMP"
  printf '%s  提示：私有仓库需提供凭据（GITHUB_TOKEN 环境变量或 ~/.git-credentials）%s\n' "$YEL" "$RST"
  exit 3
fi
