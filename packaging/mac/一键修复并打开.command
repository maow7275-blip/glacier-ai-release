#!/bin/bash
# 一键去除 macOS 隔离标记并打开 GlacierAI
# 双击此文件即可

set -e

echo "=========================================="
echo "  GlacierAI macOS 一键修复脚本"
echo "=========================================="
echo ""

APP_NAME="GlacierAI.app"

# 优先找 /Applications 下的，其次找当前 dmg 挂载目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CANDIDATES=(
    "/Applications/$APP_NAME"
    "$SCRIPT_DIR/$APP_NAME"
    "$HOME/Applications/$APP_NAME"
    "$HOME/Downloads/$APP_NAME"
)

TARGET=""
for p in "${CANDIDATES[@]}"; do
    if [ -d "$p" ]; then
        TARGET="$p"
        break
    fi
done

if [ -z "$TARGET" ]; then
    echo "[错误] 未找到 GlacierAI.app"
    echo ""
    echo "请先把 GlacierAI.app 拖到下列任一位置后再运行本脚本："
    echo "  - /Applications（推荐）"
    echo "  - 当前文件夹"
    echo "  - ~/Applications"
    echo "  - ~/Downloads"
    echo ""
    read -n 1 -s -r -p "按任意键关闭..."
    exit 1
fi

echo "找到应用：$TARGET"
echo "正在去除隔离标记..."
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true

echo "正在重新签名（ad-hoc）..."
codesign --force --deep --sign - "$TARGET" 2>/dev/null || true

echo ""
echo "✅ 已修复，可以正常使用。"
echo "正在为您打开 GlacierAI..."
open "$TARGET"

sleep 2
exit 0
