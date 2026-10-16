#!/bin/bash
# ============================================================
# 01_check_env.sh  ——  采集环境自检
# 用途：每次开工前跑一遍，确认这台 Mac 具备采集条件
# 用法：bash scripts/01_check_env.sh
# ============================================================

set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ESPECTRE_DIR="$PROJECT_ROOT/tools/espectre"
ESPECTRE_BIN="$ESPECTRE_DIR/espectre"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

pass=0
fail=0
ok()   { echo "  [OK]   $1"; pass=$((pass + 1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail + 1)); }
warn() { echo "  [WARN] $1"; }

echo "============================================================"
echo " RSS / CSI 采集环境自检"
echo " 项目目录: $PROJECT_ROOT"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo

echo "【1】操作系统"
echo "     $(sw_vers -productName) $(sw_vers -productVersion) / 处理器架构 $(uname -m)"
echo

echo "【2】Google Chrome（浏览器烧录固件、实时看 CSI 波形必需）"
if [ -x "$CHROME" ]; then
  ok "已安装: $("$CHROME" --version 2>/dev/null)"
else
  bad "未找到 Google Chrome。安装命令: brew install --cask google-chrome"
fi
echo

echo "【3】ESPectre 命令行工具"
if [ -x "$ESPECTRE_BIN" ]; then
  ok "启动脚本: $ESPECTRE_BIN"
else
  bad "缺少 $ESPECTRE_BIN"
fi
if [ -x "$ESPECTRE_DIR/.venv/bin/python" ]; then
  ok "Python 虚拟环境: $("$ESPECTRE_DIR/.venv/bin/python" -V 2>&1)"
else
  bad "缺少虚拟环境。执行: cd tools/espectre && python3.14 -m venv .venv"
fi
echo

echo "【4】本机网络"
WIFI_IF="$(networksetup -listallhardwareports 2>/dev/null | awk '/Hardware Port: Wi-Fi/{getline; print $2}')"
WIFI_IF="${WIFI_IF:-en0}"
LOCAL_IP="$(ipconfig getifaddr "$WIFI_IF" 2>/dev/null || true)"
if [ -n "$LOCAL_IP" ]; then
  ok "Wi-Fi 接口 $WIFI_IF 已连接，本机 IP = $LOCAL_IP"
  echo "         网段 = $(echo "$LOCAL_IP" | cut -d. -f1-3).x"
  echo "         提示：ESP32 拿到 IP 后必须与本机处于同一网段"
else
  bad "接口 $WIFI_IF 没有 IP，请先连上路由器 Wi-Fi"
fi
echo

echo "【5】局域网内的 ESPectre 设备"
if [ -x "$ESPECTRE_BIN" ]; then
  echo "     正在搜索（最多 6 秒）..."
  if DEVICES_OUT="$("$ESPECTRE_BIN" devices --timeout 6 2>&1)"; then
    if echo "$DEVICES_OUT" | grep -qiE "no (espectre )?devices|not found|none|0 device"; then
      warn "当前没有发现设备（正常：还没插板子或还没配网）"
    else
      ok "发现设备："
      echo "$DEVICES_OUT" | sed 's/^/         /'
    fi
  else
    warn "搜索未完成，输出如下："
    echo "$DEVICES_OUT" | sed 's/^/         /'
  fi
fi
echo

echo "【6】USB 串口（首次烧录固件时才需要）"
SERIALS="$(ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/tty.usbmodem* /dev/tty.usbserial* 2>/dev/null || true)"
if [ -n "$SERIALS" ]; then
  ok "检测到串口设备："
  echo "$SERIALS" | sed 's/^/         /'
else
  warn "未检测到 USB 串口（已烧录过的板子可以直接用 Wi-Fi，不必插线）"
fi
echo

echo "============================================================"
echo " 结果: $pass 项通过, $fail 项失败"
if [ "$fail" -eq 0 ]; then
  echo " 结论: 环境就绪，可以开始采集"
else
  echo " 结论: 请先按上面的 [FAIL] 提示修复"
fi
echo "============================================================"

exit "$fail"
