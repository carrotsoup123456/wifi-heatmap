#!/bin/bash
# 启动「固定网址共享」：userspace 模式的 tailscaled + Funnel（443 → 本机 8000）
# Mac 重启后跑一次即可；平时 tailscaled 常驻就不用管。
# 前提：已 brew install tailscale，且首次已按提示完成登录与 Funnel 授权。
set -e
TS_DIR="$HOME/.local/state/tailscale"
mkdir -p "$TS_DIR"

if ! pgrep -f "tailscaled --tun=userspace" >/dev/null; then
  echo "启动 tailscaled（userspace 模式）..."
  nohup tailscaled --tun=userspace-networking --statedir "$TS_DIR" \
       --socket "$TS_DIR/tailscaled.sock" > "$TS_DIR/daemon.log" 2>&1 &
  sleep 3
fi

tailscale --socket "$TS_DIR/tailscaled.sock" funnel --bg --https=443 8000 >/dev/null

DNS=$(tailscale --socket "$TS_DIR/tailscaled.sock" status --json \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
echo ""
echo "共享网址（固定不变）：https://$DNS"
echo "注意：本机 8000 的采集网站也要在跑（bash scripts/07_start_web.sh）"
