#!/bin/bash
# install.sh — 一键安装：体检 → 修复 → 装守护，全程无需操作
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY=""
for c in python3 /usr/bin/python3; do
    command -v "$c" >/dev/null 2>&1 && PY="$c" && break
done
[ -z "$PY" ] && echo "❌ 没找到 python3，请先安装 Python" && exit 1

echo "══════════════════════════════════════════════"
echo "  Codex Doctor 一键安装"
echo "  （体检 → 修复 → 装自动守护，全程自动）"
echo "══════════════════════════════════════════════"
echo ""
"$PY" "$DIR/scripts/codex_guard.py" install
echo ""
echo "完成。以后切路由 / 升级客户端都不用再管。"
echo "想再看一眼状态：python3 $DIR/scripts/codex_guard.py doctor"
