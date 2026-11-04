#!/bin/bash
# AI Studio · Visual Studio — 一键拉起工作台后端
# 双击本文件即可；关闭这个终端窗口会同时停止两个服务。
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

echo "=============================================================="
echo "  AI STUDIO / VISUAL STUDIO — LOCAL ATELIER"
echo "=============================================================="
echo "  工作目录：$ROOT"
echo ""
echo "  [1/2] 本地编排服务  → http://127.0.0.1:8765"
echo "  [2/2] 视频适配服务  → http://127.0.0.1:8766"
echo ""
echo "  注意：出图引擎默认以 --engine cloud 启动（DEV ONLY）。"
echo "  它会联网向 Pollinations 请求真图，仅用于课堂演示出像素；"
echo "  作业正式方案是纯端侧，请把 cloud 改成 mock 或 local。"
echo ""
echo "  视频不需要在终端配置密钥：请在工作台页面的"
echo "  「连接设置」里填入 RunningHub Key，页面会随请求一起传给适配器。"
echo ""

# 避免重复启动占住端口
for p in 8765 8766; do
  if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  ⚠ 端口 $p 已被占用，跳过对应服务（若已是本项目的服务则无需处理）。"
  fi
done

P1=""; P2=""
if ! lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  python3 "$ROOT/03_Dify工作流与API接入/local_api_server.py" \
    --engine cloud --max-images 1 --port 8765 &
  P1=$!
fi
if ! lsof -nP -iTCP:8766 -sTCP:LISTEN >/dev/null 2>&1; then
  python3 "$ROOT/06_视频API接入_演示增强/video_api_adapter.py" --serve 8766 &
  P2=$!
fi

sleep 2
echo "  打开工作台页面…"
open "$ROOT/07_产品工作台/index.html"

cleanup() {
  echo ""
  echo "  正在停止服务…"
  [ -n "$P1" ] && kill "$P1" 2>/dev/null
  [ -n "$P2" ] && kill "$P2" 2>/dev/null
}
trap cleanup EXIT INT TERM
echo "  服务已就绪。保持此窗口开启；用完直接关闭窗口即可。"
wait
