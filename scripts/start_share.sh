#!/bin/bash
# 固定网址共享：tailscaled（userspace 模式）已注册为 LaunchAgent，登录后自动启动、
# 崩溃自动拉起，平时无需运行本脚本。仅当代理未加载时手动恢复用：
#   bash scripts/start_share.sh
set -e
TS_DIR="$HOME/.local/state/tailscale"
PLIST="$HOME/Library/LaunchAgents/com.liubokang.tailscaled-userspace.plist"
LABEL="com.liubokang.tailscaled-userspace"
UID_NOW=$(id -u)

if ! launchctl print "gui/$UID_NOW/$LABEL" >/dev/null 2>&1; then
  echo "加载 LaunchAgent..."
  mkdir -p "$TS_DIR"
  launchctl bootstrap "gui/$UID_NOW" "$PLIST"
  sleep 4
fi

tailscale --socket "$TS_DIR/tailscaled.sock" funnel --bg --https=443 8000 >/dev/null

DNS=$(tailscale --socket "$TS_DIR/tailscaled.sock" status --json \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
echo ""
echo "共享网址（固定不变）：https://$DNS"
echo "注意：本机 8000 的采集网站也要在跑（bash scripts/07_start_web.sh）"
