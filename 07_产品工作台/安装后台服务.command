#!/bin/bash
# AI Studio · 让两个本地服务常驻，之后不用再开终端
# 在 Finder 里双击本文件运行一次即可，运行完终端窗口可以直接关掉。
# 引擎默认 mock（端侧正式方案，零联网、零密钥）；要联网出真图请在工作台页面里点「联网 CLOUD」。
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
DEST="$HOME/Library/LaunchAgents"
TPL="$ROOT/07_产品工作台/launchd"
D1="$DEST/com.aistudio.local8765.plist"
D2="$DEST/com.aistudio.video8766.plist"
PY=/usr/bin/python3
S1="$ROOT/03_Dify工作流与API接入/local_api_server.py"
S2="$ROOT/06_视频API接入_演示增强/video_api_adapter.py"

echo "=============================================================="
echo "  AI STUDIO / BACKGROUND SERVICE — 让服务常驻"
echo "=============================================================="
echo "  项目目录：$ROOT"
echo ""

mkdir -p "$DEST"
for name in com.aistudio.local8765 com.aistudio.video8766; do
  SRC="$TPL/$name.plist"
  if [ ! -f "$SRC" ]; then echo "  缺少模板：$SRC"; exit 1; fi
  sed "s|__PROJECT_ROOT__|$ROOT|g" "$SRC" > "$DEST/$name.plist"
done
echo "  ✓ 开机自启配置已写入 ~/Library/LaunchAgents"

# 尝试立即注册到当前登录会话（部分环境会静默失败，没关系，下一步会兜底）
launchctl unload -w "$D1" 2>/dev/null || true
launchctl load -w "$D1" 2>/dev/null || true
launchctl unload -w "$D2" 2>/dev/null || true
launchctl load -w "$D2" 2>/dev/null || true
sleep 2

H1=$(curl -s -m 2 --noproxy '*' "http://127.0.0.1:8765/health" | head -c 1)
H2=$(curl -s -m 2 --noproxy '*' "http://127.0.0.1:8766/health" | head -c 1)

if [ -n "$H1" ] && [ -n "$H2" ]; then
  echo "  ✓ 两个服务已由系统托管，开机 / 重新登录自动运行"
else
  echo "  · 系统启动项本次未立即生效（常见于非图形登录会话）"
  echo "  · 配置仍在，下次重新登录 macOS 会自动启动；现在先无窗口拉起服务"
  for p in 8765 8766; do
    PIDS=$(lsof -t -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null || true)
    [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
  done
  sleep 1
  nohup "$PY" "$S1" --engine mock --port 8765 > /tmp/aistudio-8765.out.log 2>&1 &
  nohup "$PY" "$S2" --serve 8766 > /tmp/aistudio-8766.out.log 2>&1 &
  sleep 2
  echo "  ✓ 服务已在后台运行，关闭本窗口不会中断"
fi

echo ""
echo "  状态检查："
echo "  8765 → $(curl -s -m 3 --noproxy '*' 'http://127.0.0.1:8765/health' | head -c 120)"
echo "  8766 → $(curl -s -m 3 --noproxy '*' 'http://127.0.0.1:8766/health' | head -c 120)"
echo ""
echo "  完成。以后直接打开工作台页面即可：默认演示模式零密钥、零终端、零后端依赖。"
echo "  想真出图时，在页面「连接设置 → Engine」点「联网 CLOUD」，不用改启动参数。"
echo "  卸载：双击同目录「卸载后台服务.command」"
echo "  这个终端窗口现在可以关掉了。"
