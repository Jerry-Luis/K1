#!/usr/bin/env bash
# ============================================================================
# 6指标估值模型 V3 — 全流程入口
#
# 前置自检（任一失败即非零退出，不静默产出空结果）：
#   1. python3.11 可用且依赖齐全（requests / openpyxl）
#   2. config/model_v3.json 与 model_v3.md 存在
#   3. 核心脚本文件齐全
#   4. 输出目录可写
#   5. 数据源连通（新浪快照 + 东财财务探活）
#   6. git 仓库完整（持久化校验）
#
# 用法：
#   bash run_screen.sh                 # 完整流程
#   bash run_screen.sh --limit 30      # 只跑前30只（调试）
#   bash run_screen.sh --dry-run       # 只粗筛不估值
#   bash run_screen.sh --skip-netcheck # 跳过联网自检
# ============================================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE" || exit 1

REPORT_DIR="/workspace/报告"
LOG_DIR="$BASE/logs"
DATE_ARG="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/run_${DATE_ARG//-/}.log"

mkdir -p "$LOG_DIR" "$REPORT_DIR" "$BASE/cache"

# 保存原始参数（后续 set -- 会覆盖位置参数）
ORIG_ARGS=("$@")

# 同时输出到终端与日志
exec > >(tee -a "$LOG_FILE") 2>&1

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
fail() { printf '%s✗ 自检失败：%s%s\n' "$RED" "$1" "$RST"; printf '%s\n' "退出码 3（环境不满足）"; exit 3; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$1"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$1"; }

printf '%s\n' "════════════════════════════════════════════════════════════"
printf '%s  6指标估值模型 V3 — 选股重算  %s%s\n' "$BLD" "$(date '+%Y-%m-%d %H:%M:%S')" "$RST"
printf '%s\n' "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------- 0. 持久化引导
# 沙箱每次调度可能为全新实例；若本地文件残缺，先尝试从远端恢复
# 注意：run_screen.sh 自身存在才能走到这里，其余文件缺失时 bootstrap 负责补
if [ -f "$BASE/bootstrap.sh" ]; then
  if ! bash "$BASE/bootstrap.sh"; then
    fail "持久化引导失败（代码缺失且无法从远端恢复，见上方缺失清单）"
  fi
else
  warn "bootstrap.sh 缺失，跳过远端恢复（仅依赖本地文件）"
fi

# 恢复 home 态配置（远端地址 / git 凭据），供 bootstrap 与推送使用
[ -x "$BASE/scripts/restore_env.sh" ] && bash "$BASE/scripts/restore_env.sh"

# ---------------------------------------------------------------- 1. Python
PY=""
for c in python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "未找到 python3（需 3.9+）"
PYV=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
ok "Python $PYV ($PY)"

for mod in requests openpyxl; do
  "$PY" -c "import $mod" >/dev/null 2>&1 || fail "缺少 Python 依赖: $mod（pip install $mod）"
done
ok "依赖齐全（requests / openpyxl）"

# ---------------------------------------------------------------- 2. 配置
[ -f "$BASE/config/model_v3.json" ] || fail "缺少 config/model_v3.json（模型定义丢失）"
[ -f "$BASE/config/model_v3.md" ]   || fail "缺少 config/model_v3.md（模型文档丢失）"
"$PY" -c "import json;d=json.load(open('$BASE/config/model_v3.json'));assert d['scoring']['weights']" \
  >/dev/null 2>&1 || fail "config/model_v3.json 格式错误或缺少 weights"
ok "模型定义完整（config/model_v3.json + .md）"

# ---------------------------------------------------------------- 3. 脚本
MISSING=""
for f in scripts/datasource.py scripts/model.py scripts/pipeline.py scripts/reporter.py; do
  [ -f "$BASE/$f" ] || MISSING="$MISSING $f"
done
[ -z "$MISSING" ] || fail "核心脚本缺失:$MISSING"
ok "核心脚本齐全（4 个模块）"

# ---------------------------------------------------------------- 4. 输出目录
[ -w "$REPORT_DIR" ] || fail "报告目录不可写: $REPORT_DIR"
[ -w "$BASE" ]       || fail "系统目录不可写: $BASE"
ok "输出目录可写（$REPORT_DIR）"

# ---------------------------------------------------------------- 5. git 完整性
if [ -d "$BASE/.git" ]; then
  BROKEN=$(git -C "$BASE" fsck --no-progress 2>&1 | grep -v '^Checking' | head -3)
  if [ -n "$BROKEN" ]; then warn "git fsck 报告: $BROKEN"; else ok "git 仓库完整（持久化可用）"; fi
else
  warn "未检测到 .git，无法从远端恢复（当前依赖本地文件）"
fi

# ---------------------------------------------------------------- 6. 网络探活
SET_NET=0
for a in "${ORIG_ARGS[@]}"; do [ "$a" = "--skip-netcheck" ] && SET_NET=1; done
SKIP_NET=$SET_NET
if [ "$SKIP_NET" -eq 0 ]; then
  printf '  探活数据源…'
  NET=$("$PY" - <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, "scripts")
import datasource as ds
try:
    rows = ds.fetch_all_a()
    n = len(rows)
    if n < 3000: print(f"WEAK {n}"); sys.exit(0)
    sym = rows[0]["symbol"]
    fin = ds.fetch_finance(sym)
    if not fin: print("FIN_FEHLT"); sys.exit(0)
    print(f"OK {n} {len(fin)}")
except Exception as e:
    print("ERR " + str(e)[:80])
PYEOF
)
  NET_TAG=${NET%% *}
  NET_REST=${NET#* }
  case "$NET_TAG" in
    OK)   ok "数据源连通（全A ${NET_REST%% *} 只，财务探活 ${NET_REST##* } 期）" ;;
    WEAK) printf '\n'; fail "全A快照异常（仅 $NET_REST 只，疑被限流）" ;;
    FIN_FEHLT) printf '\n'; fail "东财财务接口探活失败" ;;
    *)    printf '\n'; fail "数据源不可达: $NET_REST" ;;
  esac
fi

printf '%s\n' "────────────────────────────────────────────────────────────"
printf '%s 自检通过，开始执行流水线 %s\n' "$GRN" "$RST"
printf '%s\n' "────────────────────────────────────────────────────────────"

# ---------------------------------------------------------------- 执行
"$PY" scripts/pipeline.py --date "$DATE_ARG" "${ORIG_ARGS[@]}"
RC=$?

if [ $RC -ne 0 ]; then
  printf '%s✗ 流水线异常退出（RC=%d），报告可能不完整%s\n' "$RED" "$RC" "$RST"
  exit $RC
fi

# ---------------------------------------------------------------- 产物校验
MD_FILE="$REPORT_DIR/${DATE_ARG}_选股筛选_估值模型.md"
XLSX_FILE="$REPORT_DIR/${DATE_ARG}_选股筛选_估值模型.xlsx"
JSON_FILE="/workspace/选股结果.json"
VERR=0
for f in "$MD_FILE" "$XLSX_FILE" "$JSON_FILE"; do
  if [ -s "$f" ]; then ok "产物存在: $f ($(wc -c <"$f") bytes)"
  else printf '%s✗ 产物缺失或为空: %s%s\n' "$RED" "$f" "$RST"; VERR=1; fi
done

if [ -f "$JSON_FILE" ]; then
  SUMMARY=$("$PY" -c "
import json
d=json.load(open('$JSON_FILE'))
print(d['status'], d['candidate_pool_count'], d['passed_safety_margin_count'], len(d['stocks']))
" 2>/dev/null)
  read -r S1 S2 S3 S4 <<< "$SUMMARY"
  printf '\n%s\n' "════════════════════════════════════════════════════════════"
  printf '%s 完成：状态=%s 候选池=%s 通过=%s 标的数=%s%s\n' "$BLD" "${S1:-?}" "${S2:-?}" "${S3:-?}" "${S4:-?}" "$RST"
  printf '%s\n' "════════════════════════════════════════════════════════════"
fi

exit $VERR
