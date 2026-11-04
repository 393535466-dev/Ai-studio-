#!/bin/bash
# 端侧 AI Studio · 一键联网演示（DEV ONLY，不属于作业正式方案）
# 双击本文件即可：自检 → 打开总览页与电商 Demo → 启动带真出图能力的本地编排服务。
# 演示完在这个黑色窗口按 Ctrl+C 停止。

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SERVER="$ROOT/03_Dify工作流与API接入"

echo ""
echo "=================================================================="
echo "  端侧 AI Studio · 一键联网演示"
echo "  这个模式会真的出图片，仅用于你自己跑通 / 课堂演示。"
echo "  作业正式方案仍然是纯离线，云端引擎不写进任何文档正文。"
echo "=================================================================="
echo ""

python3 "$HERE/check_env.py" || true
echo ""

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  注意：8765 端口已经有服务在跑。"
  echo "  如果那是之前双击的「启动本地服务.command」（离线模式），请先关掉它的终端窗口，"
  echo "  否则这次打开的还是旧服务，Demo 里不会出真图。"
  echo ""
fi

echo "  正在打开浏览器页面……"
open "$ROOT/index.html" 2>/dev/null || true
sleep 1
open "$ROOT/01_电商素材工作流/Demo_视觉创作工作台.html" 2>/dev/null || true

echo ""
echo "  接下来在电商 Demo 右侧："
echo "    1) 勾选「接入本地 Dify 工作流」"
echo "    2) 点「测试工作流连接」→ 状态变绿"
echo "    3) 点「批量生成」→ 等约 10 秒，画布会出现真实图片"
echo ""
echo "  匿名通道每 45 秒只能出 1 张；重复点同一任务会命中本地缓存，不消耗额度。"
echo "  提示词会发给第三方服务，演示请用虚构商品，不要传真实客户素材。"
echo ""
echo "  按 Ctrl+C 停止服务。开始启动："
echo "------------------------------------------------------------------"
echo ""

cd "$SERVER" || exit 1
exec python3 local_api_server.py --engine cloud
