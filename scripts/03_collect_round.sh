#!/bin/bash
# ============================================================
# 03_collect_round.sh  ——  一轮走完全部 25 个测点
#
# 单板模式：
#   bash scripts/03_collect_round.sh <设备IP> r1
#
# 双板并行模式（推荐，时间约减半）：
#   bash scripts/03_collect_round.sh <板A的IP> <板B的IP> r1
#
# 可选环境变量：
#   DURATION=90   每个测点采集秒数（默认 60）
#   PPS=100       每秒发包数（默认 100）
#
# 脚本会按“一个批次两个测点”的顺序提示你摆板子，
# 摆好后按回车即开始采集，无需记住任何命令。
# ============================================================

set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ESPECTRE_BIN="$PROJECT_ROOT/tools/espectre/espectre"
ESPECTRE_DATA="$PROJECT_ROOT/tools/espectre/data"
SITES_CSV="$PROJECT_ROOT/scripts/sites.csv"
ARCHIVE_DIR="$PROJECT_ROOT/data/points"
LOG_DIR="$PROJECT_ROOT/.deepworks/tmp/collect_logs"

DURATION="${DURATION:-60}"
PPS="${PPS:-100}"

# ---- 解析参数 ----
if [ "$#" -lt 2 ]; then
  echo "用法:"
  echo "  单板: bash scripts/03_collect_round.sh <设备IP> <轮次名>"
  echo "  双板: bash scripts/03_collect_round.sh <板A的IP> <板B的IP> <轮次名>"
  exit 2
fi

IP_A="$1"
if [ "$#" -ge 3 ] && echo "$2" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  IP_B="$2"
  ROUND="$3"
else
  IP_B=""
  ROUND="$2"
fi

# ---- 全部 25 个测点，按“移动距离最短”的顺序排列 ----
POINTS="P11 P12 P13 P14 P21 P22 P23 P24 P31 P32 P33 P34 P41 P42 P43 P44 P51 P52 P53 P54 V01 V02 V03 V04 V05"
set -- $POINTS
POINT_LIST="$*"
TOTAL=25

lookup() {  # lookup <测点ID>  -> 输出 "block,role,x,y"
  grep -i "^$1," "$SITES_CSV" | head -1 | cut -d, -f2-5
}

run_one() {  # run_one <测点ID> <设备IP> <日志文件>
  local pid="$1" ip="$2" log="$3"
  local label row block role x y desc
  label="$(echo "$pid" | tr 'A-Z' 'a-z')"
  row="$(lookup "$pid")"
  block="$(echo "$row" | cut -d, -f1)"
  role="$(echo "$row" | cut -d, -f2)"
  x="$(echo "$row" | cut -d, -f3)"
  y="$(echo "$row" | cut -d, -f4)"
  desc="point=${pid};block=${block};role=${role};x=${x};y=${y};round=${ROUND}"

  "$ESPECTRE_BIN" collect \
    --target "$ip" \
    --label "$label" \
    --duration "$DURATION" \
    --pps "$PPS" \
    --ready-stable-seconds 5 \
    --description "$desc" >"$log" 2>&1
  return $?
}

archive_one() {  # archive_one <测点ID>
  local pid="$1"
  local label dst n
  label="$(echo "$pid" | tr 'A-Z' 'a-z')"
  dst="$ARCHIVE_DIR/$label"
  mkdir -p "$dst"
  n=0
  if [ -d "$ESPECTRE_DATA/$label" ]; then
    for f in "$ESPECTRE_DATA/$label"/*.npz; do
      [ -e "$f" ] || continue
      if [ ! -e "$dst/$(basename "$f")" ]; then
        cp "$f" "$dst/"
        n=$((n + 1))
      fi
    done
  fi
  echo "$n"
}

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR"

echo "============================================================"
echo " 一轮采集"
echo " 板A: $IP_A"
if [ -n "$IP_B" ]; then echo " 板B: $IP_B"; else echo " 板B: 未提供（单板模式）"; fi
echo " 轮次: $ROUND    每点: ${DURATION}s @ ${PPS}pps    总测点: $TOTAL"
echo "============================================================"
if [ -n "$IP_B" ]; then
  echo
  echo "注意：两块板同时采集会轻微互相占用信道，属于可控影响。"
  echo "      若最终发现某测点数据异常，可用单板模式重采该点。"
fi
echo
echo "按回车开始第 1 批；任意时刻 Ctrl+C 可中止。"
read -r _

i=0
BATCH=0
while [ "$i" -lt "$TOTAL" ]; do
  BATCH=$((BATCH + 1))
  set -- $POINT_LIST
  # 取出第 i+1 和第 i+2 个点
  IDX=1
  PA=""; PB=""
  for p in $POINT_LIST; do
    if [ "$IDX" -eq $((i + 1)) ]; then PA="$p"; fi
    if [ "$IDX" -eq $((i + 2)) ]; then PB="$p"; fi
    IDX=$((IDX + 1))
  done

  ROW_A="$(lookup "$PA")"
  echo "------------------------------------------------------------"
  echo " 第 $BATCH 批"
  echo "   板A -> ${PA}  (区块 $(echo "$ROW_A" | cut -d, -f1), x=$(echo "$ROW_A" | cut -d, -f3) m, y=$(echo "$ROW_A" | cut -d, -f4) m)"
  if [ -n "$IP_B" ] && [ -n "$PB" ]; then
    ROW_B="$(lookup "$PB")"
    echo "   板B -> ${PB}  (区块 $(echo "$ROW_B" | cut -d, -f1), x=$(echo "$ROW_B" | cut -d, -f3) m, y=$(echo "$ROW_B" | cut -d, -f4) m)"
  fi
  echo
  echo "   请把板子摆到胶带标出的对应格子中心，人退出房间，然后按回车。"
  read -r _

  LOG_A="$LOG_DIR/${ROUND}_${PA}.log"
  run_one "$PA" "$IP_A" "$LOG_A" &
  PID_A=$!
  PID_B=""
  if [ -n "$IP_B" ] && [ -n "$PB" ]; then
    LOG_B="$LOG_DIR/${ROUND}_${PB}.log"
    run_one "$PB" "$IP_B" "$LOG_B" &
    PID_B=$!
    wait "$PID_A" "$PID_B"
  else
    wait "$PID_A"
  fi

  N_A="$(archive_one "$PA")"
  echo "   ${PA}: 新增 ${N_A} 个文件"
  if [ -n "$PID_B" ]; then
    N_B="$(archive_one "$PB")"
    echo "   ${PB}: 新增 ${N_B} 个文件"
  fi

  i=$((i + 2))
done

echo
echo "============================================================"
echo " 本轮完成，数据已归档到 data/points/<测点ID>/"
echo " 建议接着跑： .venv/bin/python scripts/04_build_dataset.py"
echo "============================================================"
