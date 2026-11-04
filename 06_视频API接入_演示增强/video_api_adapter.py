#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频生成 API 异步适配器 —— 演示增强通道，独立于作业主体。

定位（务必先读）：
  作业正文要求「全部推理在本地、无云端兜底」。本文件不进作业正文，
  只用于答辩时额外演示「若放开端侧约束，链路如何扩展出真视频」。
  它默认不调用任何云端服务：没配 API Key 时返回结构化降级结果，
  明确写出原因，绝不伪装成生成成功 —— 与作业里「显式降级、不静默失败」同构。

三段式（生产级视频 API 的通用形态）：
  submit  ->  拿到 requestId / task_id（秒级返回）
  poll    ->  间隔递增轮询状态（InQueue / InProgress / Succeed / Failed）
  result  ->  拿到 video_url，交给下游审片 / 下载

用法：
  # 1) 零成本看形态（不发任何外部请求）
  python3 video_api_adapter.py --mock --prompt "柯基叼着棉花糖飘过天台"

  # 2) 真跑（先在环境变量里放 key）
  export VIDEO_API_KEY=sk-xxxxxxxx
  python3 video_api_adapter.py --provider siliconflow \
      --model "Wan-AI/Wan2.2-T2V-A14B" --prompt "柯基叼着棉花糖飘过天台"

  # 3) 起本地 HTTP 服务，让 Dify / 本地编排服务调用
  python3 video_api_adapter.py --serve 8766
  # POST http://127.0.0.1:8766/api/video/generate
  #     {"prompt": "...", "image_url": "可选，图生视频", "wait": true}
  # 云端 Dify 打不到 127.0.0.1，需要 ngrok / cloudflared 之类的公网穿透，
  # 再把 API_BASE 指向公网地址。这份适配器只负责「把异步变同步」。

零依赖：仅用 Python 标准库 urllib。
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# --------------------------------------------------------------------------
# 供应商适配表
# 说明：端点与字段名按各家公开文档整理，模型名、鉴权头以平台控制台实时为准。
#       新增平台只需在这里加一项，并在文档7的「适配点」表里登记，不动主流程。
# --------------------------------------------------------------------------
PROVIDERS = {
    "siliconflow": {
        "submit_url": "https://api.siliconflow.com/v1/video/submit",
        "status_url": "https://api.siliconflow.com/v1/video/status",
        "env_key": "VIDEO_API_KEY",
        "auth": "bearer",
        "default_model": "Wan-AI/Wan2.2-T2V-A14B",
        "id_keys": ("requestId", "request_id", "task_id"),
        "status_keys": ("status", "state"),
        "url_keys": ("video_url", "url"),
        "status_method": "POST",
        "label": "硅基流动 SiliconFlow（Wan2.x 系列，异步 submit/status）",
    },
    # 阿里百炼 / 可灵 / 即梦 / MiniMax 等：接口形态同为「提交任务 + 轮询」，
    # 但鉴权头、路径、字段名不同，需按各自文档补一条配置后启用。
}

RETRYABLE = {429, 503, 504}
DONE_OK = {"succeed", "success", "succeeded", "completed", "done"}
DONE_FAIL = {"failed", "fail", "error", "cancelled", "canceled"}
QUEUE = {"inqueue", "in_progress", "processing", "pending", "running", "queued"}

MAX_API_RETRIES = 3
MAX_WAIT_SECONDS = 15 * 60
INITIAL_POLL_SECONDS = 5
MAX_POLL_SECONDS = 20


def log(msg):
    print(msg, flush=True)


def post_json(url, payload, headers, timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_with_retry(url, payload, headers, timeout=30):
    """只对 429/503/504 与网络抖动做有界重试；400/401/403 立即失败，不重复扣费。"""
    for attempt in range(MAX_API_RETRIES + 1):
        try:
            return post_json(url, payload, headers, timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code not in RETRYABLE:
                raise RuntimeError("HTTP %s 不可重试：%s" % (e.code, body))
            if attempt == MAX_API_RETRIES:
                raise RuntimeError("HTTP %s 重试耗尽：%s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == MAX_API_RETRIES:
                raise RuntimeError("网络错误重试耗尽：%s" % e)
        delay = min(2 ** attempt + random.uniform(0, 1), 20)
        log("  [retry %d] %s 后重试" % (attempt + 1, round(delay, 1)))
        time.sleep(delay)
    raise RuntimeError("unreachable")


def pick(d, keys):
    """从不同厂商的响应里兜底取字段（含一层嵌套）。"""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k]:
            return d[k]
    for v in (d.values() if isinstance(d, dict) else []):
        if isinstance(v, dict):
            hit = pick(v, keys)
            if hit:
                return hit
        if isinstance(v, list):
            for item in v:
                hit = pick(item, keys)
                if hit:
                    return hit
    return None


def extract_video_url(payload):
    direct = pick(payload, ("video_url", "url", "video"))
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    videos = results.get("videos", []) if isinstance(results, dict) else []
    for v in videos:
        u = v.get("url") if isinstance(v, dict) else None
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def degraded(reason, extra=None):
    """显式降级：把「为什么没出视频」写进结果，绝不假装成功。"""
    out = {
        "status": "degraded",
        "degraded": True,
        "video_url": None,
        "reason": reason,
        "boundary": "视频渲染不属于端侧作业主体；本次未产生任何云端推理。",
    }
    if extra:
        out.update(extra)
    return out


def generate(prompt, provider="siliconflow", model=None, image_url=None,
             api_key=None, wait=True, mock=False):
    """返回结构化结果；调用方可以直接把它塞进 Dify 的结束节点。"""
    started = time.time()
    if mock:
        return degraded("已强制 --mock：演示降级路径，未发出任何外部请求。",
                        {"would_submit": {"prompt": prompt[:120], "provider": provider}})

    cfg = PROVIDERS.get(provider)
    if not cfg:
        return degraded("未登记的供应商 %r，请在 PROVIDERS 中补配置。" % provider)

    key = api_key or os.environ.get(cfg["env_key"], "").strip()
    if not key:
        return degraded(
            "未配置 %s，适配器按红线保持端侧降级，不调用云端视频服务。" % cfg["env_key"],
            {"provider": cfg["label"], "how_to_enable": "export %s=你的密钥" % cfg["env_key"]},
        )

    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or cfg["default_model"],
        "prompt": prompt,
    }
    if image_url:
        payload["image_url"] = image_url
        payload["image"] = image_url

    log("[1/3] submit -> %s" % cfg["submit_url"])
    try:
        submitted = request_with_retry(cfg["submit_url"], payload, headers)
    except Exception as e:
        return degraded("提交任务失败：%s" % e, {"stage": "submit"})

    request_id = pick(submitted, cfg["id_keys"])
    if not request_id:
        return degraded(
            "提交响应里没找到任务 ID（找过 %s），请按平台文档核对字段名。"
            % (", ".join(cfg["id_keys"])),
            {"stage": "submit", "raw": json.dumps(submitted, ensure_ascii=False)[:500]},
        )
    log("  requestId = %s" % request_id)

    if not wait:
        return {
            "status": "queued",
            "degraded": False,
            "request_id": request_id,
            "video_url": None,
            "reason": "wait=false：只提交任务，交给队列状态机后续轮询。",
        }

    log("[2/3] poll（间隔递增，最长 %d 分钟）" % (MAX_WAIT_SECONDS // 60))
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    interval = INITIAL_POLL_SECONDS
    last_state = "unknown"
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            st = request_with_retry(cfg["status_url"], {"requestId": request_id}, headers)
        except Exception as e:
            return degraded("轮询失败：%s" % e,
                            {"stage": "poll", "request_id": request_id})
        raw_state = str(pick(st, cfg["status_keys"]) or "unknown")
        last_state = raw_state.lower()
        log("  state = %s" % last_state)
        if last_state in DONE_OK:
            url = extract_video_url(st)
            log("[3/3] 完成，耗时 %.1fs" % (time.time() - started))
            return {
                "status": "succeeded",
                "degraded": False,
                "request_id": request_id,
                "video_url": url,
                "elapsed_s": round(time.time() - started, 1),
                "reason": None if url else "任务成功但响应里未找到视频 URL，请核对字段名。",
            }
        if last_state in DONE_FAIL:
            return degraded("云端任务失败：%s" % last_state,
                            {"stage": "poll", "request_id": request_id,
                             "raw": json.dumps(st, ensure_ascii=False)[:400]})
        if last_state not in QUEUE and last_state != "unknown":
            log("  未识别状态 %r，按队列中继续等待" % last_state)
        interval = min(int(interval * 1.5) + 1, MAX_POLL_SECONDS)

    # 本地不再等 ≠ 任务失败：保留 requestId 让别的 worker 接着轮询。
    return {
        "status": "poll_timeout",
        "degraded": False,
        "request_id": request_id,
        "video_url": None,
        "last_state": last_state,
        "reason": "本地等待上限已到，任务可能仍在队列；请用同一 requestId 续查，不要重复提交（会重复扣费）。",
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") not in ("/api/video/generate", "/api/video"):
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            inp = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            inp = {}
        result = generate(
            prompt=inp.get("prompt") or inp.get("script") or "",
            provider=inp.get("provider", "siliconflow"),
            model=inp.get("model"),
            image_url=inp.get("image_url"),
            api_key=inp.get("api_key"),
            wait=inp.get("wait", True),
            mock=bool(inp.get("mock")) or os.environ.get("VIDEO_ADAPTER_MOCK") == "1",
        )
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            body = json.dumps({"ok": True, "providers": list(PROVIDERS),
                               "key_configured": bool(os.environ.get("VIDEO_API_KEY"))},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="视频生成 API 异步适配器（演示增强）")
    ap.add_argument("--prompt", default="一只穿黄色雨衣的柯基叼着棉花糖飘过雨后天台",
                    help="文生视频提示词；图生视频再加 --image-url")
    ap.add_argument("--image-url", default=None, help="首帧图 URL（图生视频）")
    ap.add_argument("--provider", default="siliconflow", choices=list(PROVIDERS))
    ap.add_argument("--model", default=None, help="模型名，以平台控制台实际可用名为准")
    ap.add_argument("--mock", action="store_true", help="强制走显式降级，不发外部请求")
    ap.add_argument("--no-wait", action="store_true", help="只提交任务，不轮询")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT", help="起 HTTP 服务")
    args = ap.parse_args()

    if args.serve:
        log("视频适配器已监听 http://127.0.0.1:%d" % args.serve)
        log("POST /api/video/generate | GET /health")
        log("KEY 状态：%s" % ("已配置" if os.environ.get("VIDEO_API_KEY") else "未配置（将显式降级）"))
        HTTPServer(("127.0.0.1", args.serve), Handler).serve_forever()
        return

    result = generate(
        prompt=args.prompt, provider=args.provider, model=args.model,
        image_url=args.image_url, wait=not args.no_wait, mock=args.mock,
    )
    print("\n--- 结构化结果 ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("succeeded", "queued") else 1


if __name__ == "__main__":
    sys.exit(main())
