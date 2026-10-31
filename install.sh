#!/bin/bash
# install.sh — 一键安装入口
#
# 用法：
#   bash install.sh          # 体检 + 修复 + 安装自动守护（推荐）
#   bash install.sh doctor   # 只体检，不改任何东西
#   bash install.sh fix      # 只修一次，不装守护
#   bash install.sh uninstall # 卸载守护（菜单不动）
#
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"

if [ -z "$PY" ]; then
    echo "找不到 python3，请先安装 Python 3"
    exit 1
fi

CMD="${1:-install}"
exec "$PY" "$DIR/scripts/codex_guard.py" "$CMD"
