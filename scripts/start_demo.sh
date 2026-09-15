#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
demo_python="${DEMO_PYTHON:-python}"
demo_port="${DEMO_PORT:-8765}"
demo_args=()
case "${1:---replay}" in
  --replay) ;;
  --live) demo_args+=(--enable-execution) ;;
  *) echo 'Usage: bash scripts/start_demo.sh [--replay|--live]'; exit 2 ;;
esac
echo "演示入口：http://127.0.0.1:${demo_port}/demo"
echo '服务器上的浏览器可直接访问；远程访问请转发对应端口。'
exec "$demo_python" -u utils/dashboard_server.py --host 127.0.0.1 --port "$demo_port" "${demo_args[@]}"
