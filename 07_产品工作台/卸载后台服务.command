#!/bin/bash
# AI Studio · 停用后台服务 + 取消开机自启
# 只做「注销 + 归档 + 释放端口」，不删除任何文件，随时可以恢复。
# 在 Finder 里双击运行一次即可，运行完终端窗口可以直接关掉。
DEST="$HOME/Library/LaunchAgents"
ARCHIVE="$DEST/aistudio-disabled"
UID_N=$(id -u)
mkdir -p "$ARCHIVE"

echo "=============================================================="
echo "  AI STUDIO / BACKGROUND SERVICE — 取消开机自启"
echo "=============================================================="
echo ""

for name in com.aistudio.local8765 com.aistudio.video8766; do
  PLIST="$DEST/$name.plist"
  # 先从当前登录会话注销（新旧两种 launchctl 写法都试，任一成功即可）
  launchctl bootout "gui/$UID_N/$name" 2>/dev/null || true
  if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    mv -f "$PLIST" "$ARCHIVE/$name.plist"
    echo "  ✓ 已取消自启并归档：$name.plist"
  else
    echo "  · 未安装：$name.plist"
  fi
done

echo ""
echo "  释放端口 8765 / 8766（含手动启动的残留进程）…"
for p in 8765 8766; do
  PIDS=$(lsof -t -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null || true)
  [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
done
sleep 1

echo ""
echo "  —— 验证 ——"
LEFT=$(ls "$DEST"/com.aistudio.*.plist 2>/dev/null | wc -l | tr -d ' ')
echo "    ~/Library/LaunchAgents 剩余自启配置：$LEFT 个（应为 0）"
for p in 8765 8766; do
  if [ -n "$(lsof -t -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null)" ]; then
    echo "    端口 $p：仍在监听（可能由别的程序占用，可稍后再查）"
  else
    echo "    端口 $p：已释放 ✓"
  fi
done

echo ""
echo "  完成。以后开机 / 重新登录 macOS 不会再自动启动这两个服务。"
echo "  配置文件移到：$ARCHIVE（保留，不删除）"
echo "  想恢复：双击同目录「安装后台服务.command」。"
echo "  演示模式、分镜预演、Shot List、视频样片播放都不依赖服务，页面照常用。"
echo "  这个终端窗口现在可以关掉了。"
