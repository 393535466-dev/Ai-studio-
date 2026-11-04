#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
落地环境自检：用大白话告诉你这台机器能跑什么、缺什么。
只做读取与探活，不安装任何东西、不发起出图请求。

    python3 check_env.py
"""

import json
import os
import platform
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "03_Dify工作流与API接入")
API = "http://127.0.0.1:8765"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=12).stdout.strip()
    except Exception:
        return ""


def line(ok, label, detail):
    mark = "OK  " if ok else "--  "
    print("  %s%-16s %s" % (mark, label, detail))


def main():
    print("=" * 66)
    print(" 端侧 AI Studio · 落地环境自检")
    print("=" * 66)

    chip = sh("sysctl -n machdep.cpu.brand_string") or platform.processor() or "未知"
    mem_raw = sh("sysctl -n hw.memsize")
    mem = ("%.0f GB" % (int(mem_raw) / 1024 ** 3)) if mem_raw.isdigit() else "未知"
    model = sh("sysctl -n hw.model")
    gpu = sh("system_profiler SPDisplaysDataType 2>/dev/null | grep -m1 'Chipset Model' | cut -d: -f2")
    disk = sh("df -h / | tail -1 | awk '{print $4}'")
    print("\n[ 这台机器 ]")
    line(True, "机型 / 芯片", "%s · %s" % (model, chip))
    line(True, "内存", mem + ("（统一内存，不是独立显存）" if "Apple" in chip or "M" == chip[:1] else ""))
    line(True, "图形", (gpu or "集成显卡").strip() + " · 无 NVIDIA CUDA")
    line(True, "可用磁盘", disk or "未知")
    line(True, "Python", platform.python_version())

    has_cuda = bool(sh("which nvidia-smi"))
    if not has_cuda:
        print("\n  结论：没有独立显卡，文档①里那张「24GB 显存」是题目假设，不是这台机器。")
        print("        但端侧仍可落地，只是要走 Apple Metal / MLX 路线，并把档位降下来。")

    print("\n[ 交付文件 ]")
    for rel, label in [
        ("index.html", "总览导航"),
        ("01_电商素材工作流/Demo_视觉创作工作台.html", "电商 Demo"),
        ("02_短剧自动化工作流/Demo_视觉创作工作台.html", "短剧 Demo"),
        ("03_Dify工作流与API接入/local_api_server.py", "本地编排服务"),
        ("03_Dify工作流与API接入/engine_drivers.py", "出图引擎驱动"),
    ]:
        p = os.path.join(ROOT, rel)
        line(os.path.isfile(p), label, "在" if os.path.isfile(p) else "缺失：" + rel)

    print("\n[ 外网连通 ]")
    try:
        with urllib.request.urlopen("https://gen.pollinations.ai/v1/models", timeout=8) as r:
            r.read(128)
        line(True, "公网", "可达，匿名出图通道可用（每 45 秒 1 张）")
    except Exception as e:
        code = getattr(e, "code", None)
        if code:
            line(True, "公网", "可达（探测端点返回 HTTP %s，不影响出图）" % code)
        else:
            line(False, "公网", "不可达：%s" % e)

    print("\n[ 当前服务 ]")
    try:
        with urllib.request.urlopen(API + "/health", timeout=3) as r:
            h = json.loads(r.read().decode("utf-8"))
        line(True, "8765 端口", "已启动 · 引擎=%s · dev_only=%s" %
             (h.get("engine_mode"), h.get("dev_only")))
        try:
            with urllib.request.urlopen(API + "/engine", timeout=8) as r:
                e = json.loads(r.read().decode("utf-8"))
            line(e.get("ready", False), "引擎自检", e.get("detail", ""))
        except Exception as ex:
            line(False, "引擎自检", str(ex))
    except Exception:
        line(False, "8765 端口", "未启动 —— 双击「一键联网演示.command」即可")

    outs = os.path.join(SERVER_DIR, "outputs")
    n = len([f for f in os.listdir(outs) if f.lower().endswith((".jpg", ".png"))]) if os.path.isdir(outs) else 0
    line(n > 0, "已出真图", "%d 张，存于 %s" % (n, outs) if n else "还没有，联网演示后会落盘到这里")

    print("\n[ 建议 ]")
    print("  · 只想交作业        → 不用跑任何服务，压缩整个文件夹即可")
    print("  · 想看到真图片      → 双击「一键联网演示.command」，再在 Demo 里勾「接入本地 Dify 工作流」")
    print("  · 想完全断网出图    → 装 Draw Things（Mac App Store 免费）或 mflux，详见文档5 路线 C")
    print("  · 正式方案红线      → 云端引擎是 DEV ONLY，不属于作业正文，别写进文档")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
