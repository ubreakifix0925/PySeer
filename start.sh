#!/usr/bin/env bash
# PySeer 一键启动 WebUI 控制台
#
# 用法:
#   ./start.sh                    # 启动到 http://127.0.0.1:8680/ (默认启动时会自动检查/刷新游戏数据)
#   ./start.sh 9000               # 指定端口
#   PYSEER_HOST=0.0.0.0 ./start.sh   # 监听所有网卡
#   ./start.sh --update           # 启动前强制全量刷新一次游戏数据(精灵名/属性/技能/魂印/头像)
#   ./start.sh --no-update        # 跳过启动时的数据刷新(更快, 但资源随时更新, 数据可能偏旧)
#   PYSEER_NO_UPDATE=1 ./start.sh # 同上(用环境变量跳过刷新)
#
# 说明: 核心(登录/收发包/加解密/心跳)仅用 Python 标准库; vendor/unitypy(UnityPy)只用于
#       游戏数据(精灵名/属性/技能/魂印/头像)的自动更新, 缺省会由启动过程自动 pip 安装到 vendor/。
#       游戏资源随时更新, 因此**默认每次启动都会检查并按版本增量刷新**; 仅当明确要求时才跳过。
set -euo pipefail
cd "$(dirname "$0")"

HOST="${PYSEER_HOST:-127.0.0.1}"
PORT="${PYSEER_PORT:-8680}"
ARGS=(app/webui.py --host "$HOST" --port "$PORT")
NO_UPDATE="${PYSEER_NO_UPDATE:-}"
DO_FORCE=0

# 处理参数: <port> / --update / --no-update
for a in "$@"; do
  case "$a" in
    --update) DO_FORCE=1 ;;
    --no-update|-n) NO_UPDATE=1 ;;
    [0-9]*) PORT="$a"; ARGS=(app/webui.py --host "$HOST" --port "$PORT") ;;
  esac
done

# --update: 启动前强制刷新一次游戏数据
if [ "$DO_FORCE" = "1" ]; then
  echo "[PySeer] 强制刷新游戏数据 (assets_updater --force) ..."
  PYTHONPATH=vendor/unitypy python3 app/assets_updater.py --force || \
    echo "[PySeer] 数据刷新未完成(忽略, 继续启动)"
fi

# 仅当明确要求跳过时才加 --no-update (默认是刷新, 因为游戏资源随时更新)
if [ -n "$NO_UPDATE" ]; then
  ARGS+=(--no-update)
  echo "[PySeer] 已跳过启动时数据刷新 (--no-update; 数据可能偏旧)"
fi

echo "[PySeer] 启动控制台: http://$HOST:$PORT/  (Ctrl+C 退出)"
exec env PYTHONPATH="$PWD/vendor/unitypy" python3 -u "${ARGS[@]}"
