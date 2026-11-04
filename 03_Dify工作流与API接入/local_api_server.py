#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端侧 AI Studio · 本地编排服务（零依赖，标准库即可运行）

它同时扮演三个角色，方便在不装 GPU / 不装 Dify 的机器上验证整条链路：
  1. Dify 工作流发布后的 API 形态：POST /v1/workflows/run
  2. 创作应用画布调用的编排层：把一句话拆成结构化批量任务清单
  3. Dify DSL 里 HTTP 请求节点指向的「本机出图 API」：/api/ecom/generate、/api/drama/generate

【默认行为 · 作业正式方案】engine=mock：编排层可运行验证器，不加载模型权重、
不产出像素、不向外网发起任何请求。对应题目「全部推理在本地，无云端兜底」。

【可选开发模式 · 不属于作业正式方案】通过命令行显式切换，默认永远关闭：
    --engine local   接本机 ComfyUI（POST /prompt → /history → /view），推理仍在本地
    --engine cloud   接公网出图服务，供没有显卡的机器做开发 / 课堂演示
   cloud 是「显式切换的主引擎」，不是「本地失败后的云端兜底」；
   任何失败都只在本地降级，绝不自动转云。cloud_fallback 恒为 False。
   详见 04_可落地运行/文档5_无显卡与联网落地指南.html。

启动：
    python3 local_api_server.py                                   # 离线验证器
    python3 local_api_server.py --engine cloud                    # 联网真出图（零 key）
    python3 local_api_server.py --engine cloud --cloud-provider siliconflow
    python3 local_api_server.py --engine local                    # 接本机 ComfyUI
    python3 local_api_server.py --host 0.0.0.0 --port 9000        # Dify 在 Docker 里时

停止：Ctrl+C
"""

import argparse
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import engine_drivers as ED

STATE_FILE = os.path.join(HERE, "runtime_state.json")

# 内存中的运行记录；同时增量落盘，服务重启后仍可查询（呼应「不丢稿」）
RUNS = {}
MAX_RUNS = 200

# 与文档①保持一致的模型档位；全部为本地装载、可商用许可
ENGINE_FAST = "Z-Image-Turbo Q5_K_S"
ENGINE_FINE = "Qwen-Image fp8"
ENGINE_TEXT = "Qwen3-4B (Ollama)"

# 运行期配置，由 main() 解析命令行后写入；默认严格离线
CFG = {
    "engine": "mock",            # mock | local | cloud
    "provider": "pollinations",  # cloud 时的服务商
    "host": "127.0.0.1",
    "port": 8765,
    "size": 512,                # 演示用小图，出得快
    "max_images": 1,            # 单次工作流最多出几张，避免 HTTP 阻塞
    "output_dir": ED.DEFAULT_OUTPUT_DIR,
}


def now():
    return int(time.time())


def rid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:12]


def persist():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"runs": RUNS, "saved_at": now()}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def load_state():
    global RUNS
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                RUNS = json.load(f).get("runs", {}) or {}
        except (OSError, ValueError):
            RUNS = {}


def image_prompt(task, scene):
    """把结构化任务翻译成一句可出图的提示词（演示用，非真实商品素材）。"""
    raw = str(task.get("prompt") or "")
    if scene == "ecom":
        return ("professional ecommerce product photo, %s, %s, soft studio light, "
                "clean minimal background, centered composition, high detail, "
                "no text, no watermark" % (raw, task.get("style") or "minimal studio"))
    return ("cinematic film still, %s, %s, 9:16 vertical framing, consistent character, "
            "soft night lighting, shallow depth of field, no text" %
            (raw, task.get("action") or "quiet moment"))


def submit_to_local_engine(scene, tasks, opts=None):
    """
    出图引擎统一入口。三驱动由 CFG["engine"] 决定，默认 mock（作业正式方案）。

    mock  只回「已排队」收据，不加载权重、不发外网、不产出像素。
    local 提交给本机 ComfyUI（POST /prompt → /history → /view），推理全在本地。
    cloud DEV ONLY：显式开启才联网出真图；失败只在本地降级，绝不自动转云。
    """
    opts = opts or {}
    engine = CFG["engine"]
    base = {
        "accepted": len(tasks),
        "executor": "ComfyUI /prompt (本地单卡串行队列)",
        "slot": "1",
        "local_only": True,
        "cloud_fallback": False,
        "engine": ENGINE_FAST,
        "result_path": CFG["output_dir"],
        "images": [],
        "images_produced": 0,
        "engine_mode": engine,
        "dev_only": False,
    }
    if engine == "mock":
        base["note"] = ("编排层验证模式：未加载模型权重、未产出图像、未发起外网请求；"
                        "真机把 --engine local 接到本机 ComfyUI 即可。")
        return base

    # 模式标记与实际引擎保持一致，即使本次跳过出图也不能伪装成离线方案
    base["dev_only"] = engine == "cloud"
    base["local_only"] = engine != "cloud"
    base["executor"] = ("cloud dev driver: %s（DEV ONLY）" % CFG["provider"]
                        if engine == "cloud" else "本机 ComfyUI /prompt → /history → /view")
    want = str(opts.get("generate_images", "1")) not in ("0", "false", "False", "")
    if not want:
        base["note"] = "本次只做任务编排（连通性测试），按请求跳过出图；当前引擎为 %s" % engine
        return base

    images, errors = [], []
    for i, t in enumerate(tasks[: max(1, CFG["max_images"])]):
        try:
            images.append(ED.render_one(
                engine, CFG["provider"], scene,
                str(t.get("sku") or ("镜头%02d" % (i + 1))),
                image_prompt(t, scene),
                int(t.get("seed") or (1000 + i * 37)),
                CFG["size"], CFG["output_dir"], CFG["host"], CFG["port"]))
        except ED.EngineError as e:
            errors.append(str(e))
            break   # 失败不自动重试、不转云端，按本地降级策略停下并回报

    # 本地恢复：外部出图失败时复用已落盘候选，保证课堂演示不空场。
    # 不发新请求、不换服务商、不上云，符合文档③「已出候选保留」。
    if not images and errors and engine == "cloud":
        reused = ED.reuse_latest(scene, CFG["output_dir"], CFG["host"], CFG["port"])
        if reused:
            images = [reused]
            errors = ["外部出图失败（%s）→ 已按本地恢复策略复用上一张已落盘候选；"
                      "不重试、不转云端" % errors[0]]

    if engine == "cloud":
        base["executor"] = "cloud dev driver: %s（DEV ONLY，非作业正式方案）" % CFG["provider"]
        base["dev_only"] = True
        base["local_only"] = False
    else:
        base["executor"] = "本机 ComfyUI /prompt → /history → /view"
    reused = any(i.get("reused") for i in images)
    base.update({
        "images": images,
        "images_produced": len(images),
        "images_error": errors,
        "reused": reused,
        "sample_only": len(images) < len(tasks),
        "note": (("本地恢复：复用已落盘候选 %d 张，本次未发起新出图请求。" % len(images))
                 if reused else
                 ("开发/演示模式：实际产出 %d 张图并落盘；其余任务仍是排队收据。" % len(images)))
                 + "失败只在本地降级，cloud_fallback 恒为 False。",
    })
    return base


def build_ecom_plan(inp):
    """电商：把批量参数展开成按 SKU 的任务清单（与 DSL 的 code 节点输出结构一致）。"""
    product = str(inp.get("product_name") or inp.get("product") or "示例商品")
    style = str(inp.get("style") or "极简白棚")
    size = str(inp.get("size") or "1:1 主图")
    n = int(inp.get("sku_count") or inp.get("scope") or 6)
    c = int(inp.get("candidates") or inp.get("count") or 4)
    fine = str(inp.get("fine") or "0") in ("1", "true", "True")
    n = max(1, min(n, 12))
    c = max(1, min(c, 8))
    tasks = []
    for i in range(n):
        tasks.append({
            "sku": "SKU-%02d" % (i + 1),
            "name": "%s · %s · SKU-%02d" % (product, style, i + 1),
            "style": style,
            "size": size,
            "candidates": c,
            "count": c,
            "anchor_lock": True,
            "seed": 1000 + i * 37,
            "prompt": "product photo of %s, %s, anchored reference, clean composition" % (product, style),
            "negative": "deformed, watermark, extra logo, extra text",
            "engine": ENGINE_FINE if fine else ENGINE_FAST,
        })
    plan = {
        "plan_id": rid("ecom"),
        "scene": "ecom",
        "engine": ENGINE_FINE if fine else ENGINE_FAST,
        "text_model": ENGINE_TEXT,
        "anchor_lock": True,
        "quality_gate": "主体形状 / LOGO / 配色与原始商品锚图一致；中文主图文字不得糊",
        "fallback": "1024→768 → 候选数减半 → 排队 → 取消（无云端兜底）",
        "local_only": True,
        "cloud_fallback": False,
        "tasks": tasks,
    }
    return plan, tasks


def build_drama_plan(inp):
    """短剧：把剧本拆成分镜，强制 ≤5s / 图先行（与 DSL 的 code 节点输出结构一致）。"""
    script = str(inp.get("script") or "")
    n = int(inp.get("shot_count") or 6)
    resolution = str(inp.get("resolution") or "480p 预演")
    anchor_on = "关闭" not in str(inp.get("anchor_lock") or "每镜头 re-anchor（推荐）")
    n = max(1, min(n, 12))
    scene_words = ["中景", "近景", "全景", "过肩", "特写", "空镜"]
    action_words = [
        "两人对视，情绪克制", "人物缓步走入画面", "手中物件特写，光线偏冷",
        "回头，欲言又止", "城市灯光在远处虚化", "沉默，镜头微推",
        "抬头，表情松动", "转身离开，背影留白",
    ]
    lines = [l.strip() for l in script.replace("\n", " ").split("。") if l.strip()]
    shots = []
    for i in range(n):
        action = lines[i][:40] if i < len(lines) else action_words[i % len(action_words)]
        shots.append({
            "scene": scene_words[i % len(scene_words)],
            "action": action,
            "dur": min(5, 3 + i % 3),          # 端侧硬边界：单镜头 ≤5s
            "prompt": "cinematic still, character anchored reference, night convenience store, 9:16",
            "camera": "固定" if i % 2 == 0 else "缓推",
            "identity_ref": "原始角色锚图",
            "stage": "keyframe",               # 图先行：本轮只排关键帧，不启动视频
            "re_anchor": anchor_on,
            "resolution": resolution,
        })
    plan = {
        "plan_id": rid("drama"),
        "scene": "drama",
        "engine": ENGINE_FAST,
        "text_model": ENGINE_TEXT,
        "boundary": "竖屏 9:16 / 单镜头 ≤5s / 图先行 / I2V 5 帧段（无云端兜底）",
        "characters": ["林夏", "陈屿"],
        "re_anchor": anchor_on,
        "local_only": True,
        "cloud_fallback": False,
        "shots": shots,
    }
    return plan, shots


def run_workflow(payload):
    """模拟 Dify 工作流执行：开始 → LLM → 代码校验 → HTTP → 结束，返回官方回包形态。"""
    inputs = payload.get("inputs") or {}
    scene = str(inputs.get("scene") or "ecom")
    if scene == "drama":
        plan, tasks = build_drama_plan(inputs)
    else:
        plan, tasks = build_ecom_plan(inputs)
        scene = "ecom"

    receipt = submit_to_local_engine(scene, tasks, inputs)
    run_id = rid("run")
    task_id = rid("task")
    record = {
        "id": run_id,
        "task_id": task_id,
        "status": "succeeded",
        "scene": scene,
        "created_at": now(),
        "finished_at": now(),
        "elapsed_time": 0.12,
        "total_steps": 5,
        "user": payload.get("user") or "studio-local",
        "plan": plan,
        "stopped": False,
    }
    RUNS[run_id] = record
    if len(RUNS) > MAX_RUNS:
        for k in list(RUNS)[: len(RUNS) - MAX_RUNS]:
            RUNS.pop(k, None)
    persist()

    imgs = receipt.get("images", [])
    summary = ("本地编排完成：%d 个 %s，锚图锁定，单卡串行排队，无云端兜底"
               % (len(tasks), "SKU 批量任务" if scene == "ecom" else "结构化分镜"))
    if imgs:
        summary += "；开发演示模式实际出图 %d 张（%s）" % (len(imgs), CFG["provider"] if CFG["engine"] == "cloud" else "ComfyUI")
    outputs = {
        "plan": json.dumps(plan, ensure_ascii=False),
        "generate_result": json.dumps(receipt, ensure_ascii=False),
        "summary": summary,
        "task_count": len(tasks),
        "shot_count": len(tasks) if scene == "drama" else 0,
        "engine": plan["engine"],
        "local_only": receipt.get("local_only", True),
        "images": json.dumps(imgs, ensure_ascii=False),
        "images_produced": len(imgs),
        "images_error": json.dumps(receipt.get("images_error", []), ensure_ascii=False),
        "reused": receipt.get("reused", False),
        "engine_mode": CFG["engine"],
        "dev_only": receipt.get("dev_only", False),
    }
    return {
        "task_id": task_id,
        "workflow_run_id": run_id,
        "data": {
            "id": run_id,
            "workflow_id": "local-" + scene,
            "status": "succeeded",
            "outputs": outputs,
            "sample_images": imgs,
            "error": None,
            "elapsed_time": 0.12,
            "total_steps": 5,
            "total_tokens": 0,
            "created_at": now(),
            "finished_at": now(),
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AIStudioLocal/1.0"

    def log_message(self, fmt, *args):
        print("  [%s] %s" % (self.log_date_time_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _binary(self, raw, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            return self._json({
                "service": "端侧 AI Studio · 本地编排服务",
                "status": "ok",
                "engine_mode": CFG["engine"],
                "cloud_provider": CFG["provider"] if CFG["engine"] == "cloud" else None,
                "dev_only": CFG["engine"] == "cloud",
                "local_only": CFG["engine"] != "cloud",
                "cloud_fallback": False,
                "max_images": CFG["max_images"],
                "image_size": CFG["size"],
                "note": ("离线验证器：不加载权重、不产出像素、不发外网请求（作业正式方案）"
                         if CFG["engine"] == "mock" else
                         "开发/演示模式：非作业正式方案；失败只在本地降级，绝不自动转云端"),
                "endpoints": {
                    "GET /health": "健康检查",
                    "GET /engine": "引擎自检（前置条件是否满足）",
                    "GET /outputs/<file>": "取回已落盘的真实图片",
                    "POST /v1/workflows/run": "Dify 工作流 API 形态（画布 / Demo 调用）",
                    "POST /api/ecom/generate": "DSL 电商 HTTP 节点的目标",
                    "POST /api/drama/generate": "DSL 短剧 HTTP 节点的目标",
                    "POST /api/demo/generate_image": "单张出图（开发演示）",
                    "POST /v1/workflows/{task_id}/stop": "中断（对应加分项）",
                    "GET /v1/workflows/run/{run_id}": "查询运行记录（持久化）",
                },
                "runs_in_memory": len(RUNS),
            })
        if path == "/engine":
            ok, msg = ED.probe(CFG["engine"], CFG["provider"])
            return self._json({"engine": CFG["engine"], "provider": CFG["provider"],
                               "ready": ok, "detail": msg,
                               "dev_only": CFG["engine"] == "cloud",
                               "assignment_mode": "dev-demo" if CFG["engine"] == "cloud" else "formal"})
        if path.startswith("/outputs/"):
            name = os.path.basename(path)
            full = os.path.join(CFG["output_dir"], name)
            if os.path.abspath(full).startswith(os.path.abspath(CFG["output_dir"])) and os.path.isfile(full):
                ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
                with open(full, "rb") as f:
                    return self._binary(f.read(), ctype)
            return self._json({"error": "image not found", "file": name}, 404)
        if path.startswith("/v1/workflows/run/"):
            r = RUNS.get(path.rsplit("/", 1)[-1])
            return self._json(r if r else {"error": "not found"}, 200 if r else 404)
        return self._json({"error": "unknown path", "path": path}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            data = json.loads(raw or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            return self._json({"error": "invalid json", "detail": str(e)}, 400)

        if path == "/v1/workflows/run":
            return self._json(run_workflow(data))

        if path.startswith("/v1/workflows/") and path.endswith("/stop"):
            tid = path.split("/")[3]
            hit = None
            for r in RUNS.values():
                if r.get("task_id") == tid:
                    hit = r
                    break
            if hit:
                hit["status"] = "stopped"
                hit["stopped"] = True
                persist()
                return self._json({"result": "success", "run_id": hit["id"],
                                   "note": "对应 ComfyUI POST /interrupt：显存释放，已出候选保留"})
            return self._json({"result": "success", "note": "任务已结束或不存在"}, 200)

        if path in ("/api/ecom/generate", "/api/drama/generate"):
            # Dify DSL 的 HTTP 请求节点会把 code 节点生成的 request_json 直接 POST 过来
            scene = "drama" if "drama" in path else "ecom"
            tasks = data.get("tasks") or []
            plan_id = data.get("plan_id") or rid(scene)
            receipt = submit_to_local_engine(scene, tasks, data)
            receipt.update({
                "plan_id": plan_id,
                "queued": len(tasks),
                "status_code": 200,
                "anchor_lock": bool(data.get("anchor_lock", True)),
                "stage": data.get("stage") or "image",
            })
            return self._json(receipt)

        if path == "/api/demo/generate_image":
            # 开发演示：一次出一张真图，供画布逐张显示（不改变工作流编排职责）
            try:
                meta = ED.render_one(
                    CFG["engine"], CFG["provider"],
                    str(data.get("scene") or "ecom"),
                    str(data.get("sku") or "demo"),
                    str(data.get("prompt") or "professional product photo of a white ceramic mug, minimal studio"),
                    int(data.get("seed") or 42),
                    int(data.get("size") or CFG["size"]),
                    CFG["output_dir"], CFG["host"], CFG["port"])
                return self._json({"ok": True, "image": meta})
            except ED.EngineError as e:
                return self._json({"ok": False, "error": str(e),
                                   "engine_mode": CFG["engine"],
                                   "hint": "mock 模式按设计不出图；启动时加 --engine cloud 或 --engine local"})

        return self._json({"error": "unknown path", "path": path}, 404)


def main():
    ap = argparse.ArgumentParser(description="端侧 AI Studio 本地编排服务")
    ap.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1；Dify 在 Docker 里时用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--engine", choices=["mock", "local", "cloud"], default="mock",
                    help="默认 mock（作业正式方案，纯离线）；cloud 仅供无显卡机器开发演示")
    ap.add_argument("--cloud-provider", choices=["pollinations", "siliconflow"],
                    default="pollinations", help="cloud 时的服务商，默认零 key 的 pollinations")
    ap.add_argument("--image-size", type=int, default=512, help="演示出图边长，默认 512")
    ap.add_argument("--max-images", type=int, default=1, help="单次工作流最多出几张，默认 1")
    ap.add_argument("--public-host", default="127.0.0.1", help="图片 URL 里用的主机名")
    args = ap.parse_args()

    CFG.update({
        "engine": args.engine,
        "provider": args.cloud_provider,
        "host": args.public_host,
        "port": args.port,
        "size": max(128, min(args.image_size, 1024)),
        "max_images": max(1, min(args.max_images, 8)),
    })
    os.makedirs(CFG["output_dir"], exist_ok=True)
    load_state()
    ok, msg = ED.probe(CFG["engine"], CFG["provider"])
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 70)
    print(" 端侧 AI Studio · 本地编排服务已启动")
    print(" 监听：http://%s:%d" % (args.host, args.port))
    print(" 引擎：%s%s   自检：%s" % (CFG["engine"],
          (" / " + CFG["provider"]) if CFG["engine"] == "cloud" else "",
          "通过 · " + msg if ok else "未就绪 · " + msg))
    if CFG["engine"] == "mock":
        print(" 模式：作业正式方案 · 纯离线 · 不加载权重 · 不产出像素 · 不发外网请求")
    elif CFG["engine"] == "local":
        print(" 模式：真机端侧 · 推理全部在本机 ComfyUI 完成")
    else:
        print(" 模式：DEV ONLY 开发演示（不属于作业正式方案，请勿写进作业正文）")
        print(" 说明：显式切换的主引擎，不是兜底；cloud_fallback 恒为 False")
        print(" 隐私：提示词会发给第三方服务，演示请用虚构商品，勿传真实素材")
    print(" 健康检查：curl http://127.0.0.1:%d/health" % args.port)
    print(" 引擎自检：curl http://127.0.0.1:%d/engine" % args.port)
    print(" 工作流调用：curl -X POST http://127.0.0.1:%d/v1/workflows/run \\" % args.port)
    print("              -H 'Content-Type: application/json' \\")
    print("              -d '{\"inputs\":{\"scene\":\"ecom\",\"product_name\":\"保温杯\"}}'")
    print(" 图片目录：%s" % CFG["output_dir"])
    print(" 停止：Ctrl+C    运行记录：runtime_state.json")
    print("=" * 70)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。运行记录保持在 runtime_state.json")
    finally:
        persist()
        srv.server_close()


if __name__ == "__main__":
    main()
