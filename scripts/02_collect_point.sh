#!/bin/bash
# ============================================================
# 02_collect_point.sh  ——  采集单个测点
#
# 用法：
#   bash scripts/02_collect_point.sh <测点ID> <设备IP> [秒数] [pps] [轮次]
#
# 例子：
#   bash scripts/02_collect_point.sh P11 192.168.1.50
#   bash scripts/02_collect_point.sh V03 192.168.1.50 90 100 r2
#
# 测点ID 必须是 scripts/sites.csv 里登记过的，例如 P11 / P54 / V01
# ============================================================

set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ESPECTRE_BIN="$PROJECT_ROOT/tools/espectre/espectre"
ESPECTRE_DATA="$PROJECT_ROOT/tools/espectre/data"
SITES_CSV="$PROJECT_ROOT/scripts/sites.csv"
ARCHIVE_DIR="$PROJECT_ROOT/data/points"

POINT_ID="${1:-}"
DEVICE_IP="${2:-}"
DURATION="${3:-60}"
PPS="${4:-100}"
ROUND="${5:-r1}"

if [ -z "$POINT_ID" ] || [ -z "$DEVICE_IP" ]; then
  echo "用法: bash scripts/02_collect_point.sh <测点ID> <设备IP> [秒数] [pps] [轮次]"
  echo "例:   bash scripts/02_collect_point.sh P11 192.168.1.50"
  exit 2
fi

# ---- 1. 查测点坐标 ----
POINT_UPPER="$(echo "$POINT_ID" | tr 'a-z' 'A-Z')"
SITE_ROW="$(grep -i "^${POINT_UPPER}," "$SITES_CSV" 2>/dev/null || true)"
if [ -z "$SITE_ROW" ]; then
  echo "[错误] 测点 '$POINT_ID' 不在 $SITES_CSV 中。"
  echo "       可用测点：P11-P54（20 个训练点）, V01-V05（5 个验证点）"
  exit 2
fi

BLOCK="$(echo "$SITE_ROW" | cut -d, -f2)"
ROLE="$(echo "$SITE_ROW" | cut -d, -f3)"
X_M="$(echo "$SITE_ROW" | cut -d, -f4)"
Y_M="$(echo "$SITE_ROW" | cut -d, -f5)"

LABEL="$(echo "$POINT_UPPER" | tr 'A-Z' 'a-z')"   # 采集器要求 label 为小写 ASCII
DESCRIPTION="point=${POINT_UPPER};block=${BLOCK};role=${ROLE};x=${X_M};y=${Y_M};round=${ROUND}"

echo "============================================================"
echo " 采集测点 $POINT_UPPER  ($BLOCK / $ROLE)"
echo " 坐标: x=${X_M} m, y=${Y_M} m        轮次: $ROUND"
echo " 设备: $DEVICE_IP      时长: ${DURATION}s     速率: ${PPS} pps"
echo "============================================================"
echo

# ---- 2. 打时间戳（用于事后挑出“本次新增”的文件）----
STAMP_DIR="$PROJECT_ROOT/.deepworks/tmp"
mkdir -p "$STAMP_DIR"
STAMP="$STAMP_DIR/.collect_stamp_$$"
touch "$STAMP"

# ---- 3. 调用官方采集器 ----
echo "[1/3] 连接设备并采集（请保持房间无人、静止）..."
"$ESPECTRE_BIN" collect \
  --target "$DEVICE_IP" \
  --label "$LABEL" \
  --duration "$DURATION" \
  --pps "$PPS" \
  --ready-stable-seconds 5 \
  --description "$DESCRIPTION"
COLLECT_RC=$?

if [ "$COLLECT_RC" -ne 0 ]; then
  echo
  echo "[失败] 采集器返回码 $COLLECT_RC。常见原因："
  echo "       - IP 写错 / 设备不在同一网段  -> 先跑 bash scripts/01_check_env.sh"
  echo "       - 设备正忙或刚重启              -> 等 20 秒重试"
  echo "       - 房间里有人走动导致不满足静止条件 -> 清场后重试"
  exit "$COLLECT_RC"
fi

# ---- 4. 把本次新增的 npz 归档到项目 data/points/<label>/ ----
echo
echo "[2/3] 归档数据..."
mkdir -p "$ARCHIVE_DIR/$LABEL"
COUNT=0
if [ -d "$ESPECTRE_DATA/$LABEL" ]; then
  for f in "$ESPECTRE_DATA/$LABEL"/*.npz; do
    [ -e "$f" ] || continue
    [ "$f" -nt "$STAMP" ] || continue
    cp "$f" "$ARCHIVE_DIR/$LABEL/"
    COUNT=$((COUNT + 1))
    echo "       -> data/points/$LABEL/$(basename "$f")"
  done
fi
rm -f "$STAMP"

# ---- 5. 摘要 ----
echo
echo "[3/3] 完成"
if [ "$COUNT" -eq 0 ]; then
  echo "       [警告] 没有发现新文件，请检查上面的采集器输出"
  exit 1
fi
echo "       本次新增 $COUNT 个数据文件，累计 $(ls "$ARCHIVE_DIR/$LABEL" 2>/dev/null | wc -l | tr -d ' ') 个"
echo
echo " 下一个动作：把板子移到下一个测点，再跑一次本脚本。"
