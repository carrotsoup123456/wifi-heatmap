#!/bin/bash
# Wi-Fi 热力图 · 一键采集网站启动器
# 用法: bash scripts/07_start_web.sh   然后浏览器打开 http://127.0.0.1:8000
set -euo pipefail

cd "$(dirname "$0")/.."
PY="tools/espectre/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "找不到虚拟环境 Python: $PY"
  echo "请先运行 scripts/01_check_env.sh 检查环境"
  exit 1
fi

# 若 8000 端口已被占用（比如已经启动过），直接提示
if lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "网站已经在运行了：http://127.0.0.1:8000"
  exit 0
fi

exec "$PY" web/app.py
