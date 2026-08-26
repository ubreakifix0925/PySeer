#!/usr/bin/env bash
# PySeer 一键"安装并启动" —— 适用于**尚未下载项目**的机器.
#
# 从零一键(无需先下载本项目):
#   bash <(curl -fsSL https://raw.githubusercontent.com/ubreakifix0925/PySeer/main/deploy.sh)
#   或(已拿到 deploy.sh): ./deploy.sh [端口] [--update]
#
# 环境变量:
#   PYSEER_REPO=<git仓库>   # 默认 GitHub 仓库; 可改成自己的镜像
#   PYSEER_DIR=<目录>        # 下载到的目标目录 (默认 PySeer)
#   PYSEER_HOST / PYSEER_PORT / PYSEER_NO_UPDATE=1   # 透传给 start.sh
#
# 说明: 项目仅需 Python 3.8+(纯标准库); 本脚本会: ① 检查/安装 python3; ② git clone(或下载 zip)
#       到 ./PySeer; ③ 执行 ./start.sh 启动 WebUI 控制台.
set -euo pipefail

REPO_URL="${PYSEER_REPO:-https://github.com/ubreakifix0925/PySeer.git}"
REPO_BASE="${REPO_URL%.git}"
TARGET_DIR="${PYSEER_DIR:-PySeer}"
BRANCH="main"

echo "[PySeer] 一键安装并启动 (repo=$REPO_URL -> ./$TARGET_DIR)"

# ---------- ① 检查并安装 python3 ----------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[PySeer] 未找到 python3, 尝试安装 ..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3 git curl tar
  elif command -v brew >/dev/null 2>&1; then
    brew install python3 git
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 git curl tar
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm python git curl tar
  else
    echo "[PySeer] 无法自动安装 python3, 请手动安装 Python ≥ 3.8 后重试。"
    exit 1
  fi
fi
command -v python3 >/dev/null 2>&1 || { echo "[PySeer] python3 仍不可用, 请手动安装 Python ≥ 3.8。"; exit 1; }
echo "[PySeer] 使用: $(command -v python3) ($(python3 --version 2>&1))"

# ---------- ② 下载项目 ----------
if [ -d "$TARGET_DIR/.git" ]; then
  echo "[PySeer] $TARGET_DIR 已是本仓库, 跳过下载"
elif [ -d "$TARGET_DIR" ] && [ -f "$TARGET_DIR/start.sh" ]; then
  echo "[PySeer] $TARGET_DIR 已存在(含 start.sh), 直接使用"
else
  if command -v git >/dev/null 2>&1; then
    echo "[PySeer] git clone ..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
  else
    echo "[PySeer] 无 git, 改用下载源码包 ..."
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$REPO_BASE/archive/refs/heads/$BRANCH.tar.gz" -o /tmp/pyseer.tgz
    elif command -v wget >/dev/null 2>&1; then
      wget -qO /tmp/pyseer.tgz "$REPO_BASE/archive/refs/heads/$BRANCH.tar.gz"
    else
      echo "[PySeer] 需要 curl 或 wget(或安装 git) 才能下载。"; exit 1
    fi
    echo "[PySeer] 解压 ..."
    rm -rf /tmp/pyseer_dl && mkdir -p /tmp/pyseer_dl
    tar -xzf /tmp/pyseer.tgz -C /tmp/pyseer_dl
    # 解出的目录名形如 <repo>-<branch>; 取其唯一子目录
    SRC="$(find /tmp/pyseer_dl -mindepth 1 -maxdepth 1 -type d | head -n1)"
    rm -rf "$TARGET_DIR" && mv "$SRC" "$TARGET_DIR"
    rm -rf /tmp/pyseer_dl /tmp/pyseer.tgz
  fi
fi
cd "$TARGET_DIR"

# ---------- ③ 启动 ----------
echo "[PySeer] 准备启动控制台 ..."
exec ./start.sh "$@"
