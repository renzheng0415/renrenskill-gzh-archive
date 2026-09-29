#!/usr/bin/env bash
# gzh-archive 的入口包装器
#
# 作用：不管从哪个目录调用，都能正确定位 skill 自带的代码，
#      并把参数原样透传给 gzh CLI。
#
# 用法：
#   bash <skill>/scripts/gzh.sh crawl --from-url "https://mp.weixin.qq.com/s/xxx"
#   bash <skill>/scripts/gzh.sh article "https://mp.weixin.qq.com/s/xxx"
#   bash <skill>/scripts/gzh.sh --help
#
# 为什么要有这层：agent 调用时 cwd 不确定，而 `python3 -m gzh` 依赖
# PYTHONPATH 里有 skill 的 scripts 目录。这层把它钉死，避免"模块找不到"。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 找可用的 python（skill 只依赖标准库 + requests）
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到 python3。请先安装 Python 3.9+。" >&2
  exit 2
fi

# requests 是唯一的外部依赖
if ! "$PY" -c "import requests" >/dev/null 2>&1; then
  echo "⚠️  缺少 requests 库，正在安装…" >&2
  "$PY" -m pip install --quiet requests 2>/dev/null || {
    echo "❌ 安装 requests 失败。请手动执行：$PY -m pip install requests" >&2
    exit 2
  }
fi

export PYTHONPATH="$BASE_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m gzh "$@"
