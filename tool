#!/usr/bin/env sh
# DevToolRadar CLI 入口（macOS / Linux）。Windows 用同目录的 tool.cmd。
#
# 用法：
#   ./tool find "造测试数据"
#   ln -s "$PWD/tool" /usr/local/bin/tool    # 想要全局可用时
#
# 想指定解释器就设 DEVTOOLRADAR_PYTHON，否则依次尝试 python3 / python。
set -e
DIR=$(cd "$(dirname "$0")" && pwd)

if [ -n "$DEVTOOLRADAR_PYTHON" ]; then
    exec "$DEVTOOLRADAR_PYTHON" "$DIR/src/cli.py" "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 "$DIR/src/cli.py" "$@"
else
    exec python "$DIR/src/cli.py" "$@"
fi
