#!/bin/bash
# AI Studio · 停用后台服务（只停用与归档，不删除任何文件）
DEST="$HOME/Library/LaunchAgents"
ARCHIVE="$DEST/aistudio-disabled"
mkdir -p "$ARCHIVE"

echo "=============================================================="
echo "  AI STUDIO / BACKGROUND SERVICE — 停用后台服务"
echo "=============================================================="

for name in com.aistudio.local8765 com.aistudio.video8766; do
  PLIST="$DEST/$name.plist"
  if [ -f "$PLIST" ]; then
    launchctl unload -w "$PLIST" 2>/dev/null || true
    mv "$PLIST" "$ARCHIVE/$name.plist"
    echo "  已停用并归档：$name.plist"
  else
    echo "  未安装：$name.plist"
  fi
done

echo ""
echo "  释放端口…"
for p in 8765 8766; do
  PIDS=$(lsof -t -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null || true)
  [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
done
echo "  配置文件已移到 $ARCHIVE（保留，随时可以移回来重装）。"
echo "  端口 8765 / 8766 已释放；工作台的演示模式与分镜预演不受影响。"
echo "  这个终端窗口现在可以关掉了。"
