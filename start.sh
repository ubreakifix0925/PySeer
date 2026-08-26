#!/usr/bin/env bash
# PySeer 一键启动 WebUI 控制台
#
# 用法:
#   ./start.sh                # 启动到 http://127.0.0.1:8680/
#   ./start.sh 9000           # 指定端口
#   PYSEER_HOST=0.0.0.0 ./start.sh   # 监听所有网卡
#   ./start.sh --update      # 强制先跑一遍数据自更新(精灵名/属性/技能/魂印/头像)
#   PYSEER_NO_UPDATE=1 ./start.sh    # 跳过启动时的数据自更新(更快)
#
# 说明: 项目仅需 Python 标准库即可运行; vendor/unitypy 仅用于游戏数据自更新(可选)。
#       若缺失 vendor/unitypy, 会自动加 --no-update 以保证一键可跑(精灵名可能显示"未知")。
set -euo pipefail
cd "$(dirname "$0")"

HOST="${PYSEER_HOST:-127.0.0.1}"
PORT="${PYSEER_PORT:-8680}"
ARGS=(app/webui.py --host "$HOST" --port "$PORT")
NO_UPDATE="${PYSEER_NO_UPDATE:-}"

# 处理显式端口参数: ./start.sh <port>
if [ "$#" -ge 1 ] && [ "$1" != "--update" ] && [[ "$1" =~ ^[0-9]+$ ]]; then
  PORT="$1"; ARGS=(app/webui.py --host "$HOST" --port "$PORT")
fi

# --update: 启动前强制刷新一次游戏数据
if [ "$#" -ge 1 ] && [ "$1" = "--update" ]; then
  echo "[PySeer] 强制刷新游戏数据 (assets_updater --force) ..."
  PYTHONPATH=vendor/unitypy python3 app/assets_updater.py --force || \
    echo "[PySeer] 数据刷新未完成(忽略, 继续启动; 可先装 vendor/unitypy 或 ./start.sh --update)"
fi

# 缺失 vendor/unitypy -> 跳过数据自更新, 保证一键可跑
if [ ! -d vendor/unitypy ]; then
  ARGS+=(--no-update)
  [ -n "$NO_UPDATE" ] || echo "[PySeer] 未检测到 vendor/unitypy, 已跳过启动时数据自更新(需要时: ./start.sh --update)"
fi

echo "[PySeer] 启动控制台: http://$HOST:$PORT/  (Ctrl+C 退出)"
exec env PYTHONPATH="$PWD/vendor/unitypy" python3 -u "${ARGS[@]}"
