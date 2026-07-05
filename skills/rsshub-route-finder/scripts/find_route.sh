#!/bin/bash
# RSSHub Route Finder - Shell Wrapper
# 用法: ./find_route.sh <URL>

if [ -z "$1" ]; then
    echo "❌ 用法: $0 <URL>"
    echo "示例: $0 'https://github.com/DIYgod/RSSHub'"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
uv run --script "$SCRIPT_DIR/find_route.py" "$1"
