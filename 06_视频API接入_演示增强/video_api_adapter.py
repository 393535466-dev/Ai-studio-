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
  submit  ->  拿到 taskId / requestId（秒级返回）
  poll    ->  间隔递增轮询状态（QUEUED / RUNNING / SUCCESS / FAILED）
  result  ->  拿到视频 URL，交给下游审片 / 下载

支持的通道：
  siliconflow          硅基流动 /video/submit + /video/status（requestId）
  runninghub           RunningHub 模型 API（Bearer + /openapi/v2/... + /openapi/v2/query）
  runninghub_workflow  RunningHub 工作流 API（body 传 apiKey + workflowId，扣个人钱包）

用法：
  # 1) 零成本看形态（不发任何外部请求）
  python3 video_api_adapter.py --mock --prompt "柯基叼着棉花糖飘过天台"

  # 2) RunningHub 模型 API（需企业级-共享 Key + 团队余额）
  export RUNNINGHUB_API_KEY=你的32位密钥
  python3 video_api_adapter.py --provider runninghub --duration 4 \
      --resolution 480P --prompt "柯基叼着棉花糖飘过天台" --save corgi.mp4

  # 3) RunningHub 工作流 API（可用消费级-会员 Key，需已保存并跑过的 workflowId）
  python3 video_api_adapter.py --provider runninghub_workflow \
      --workflow-id 1987728214757978114 --prompt "柯基叼着棉花糖飘过天台"

  # 4) 起本地 HTTP 服务，让 Dify / 本地编排服务调用
  python3 video_api_adapter.py --serve 8766
  # POST http://127.0.0.1:8766/api/video/generate
  #     {"prompt":"...", "provider":"runninghub", "duration":"4", "wait":true}
  # 云端 Dify 打不到 127.0.0.1，需要公网穿透后再把 API_BASE 指向公网地址。

零依赖：仅用 Python 标准库 urllib。密钥只从环境变量 / 参数读取，不写入文件。
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 供应商适配表
# 新增平台只需加一项配置（必要时加一个 dialect 分支），不动调用方。
# 端点与字段名按各家公开 API 文档整理，以平台控制台实时口径为准。
# --------------------------------------------------------------------------
PROVIDERS = {
    "siliconflow": {
        "dialect": "standard",
        "submit_url": "https://api.siliconflow.com/v1/video/submit",
        "status_url": "https://api.siliconflow.com/v1/video/status",
        "env_key": "VIDEO_API_KEY",
        "auth": "bearer",
        "default_model": "Wan-AI/Wan2.2-T2V-A14B",
        "id_keys": ("requestId", "request_id", "task_id"),
        "status_keys": ("status", "state"),
        "status_method": "POST",
        "label": "硅基流动 SiliconFlow（Wan2.x 系列，异步 submit/status）",
    },
    "runninghub": {
        "dialect": "runninghub_v2",
        "base": "https://www.runninghub.ai",
        "default_endpoint": "/openapi/v2/alibaba/wan-3.0-prime/text-to-video",
        "image_endpoint": "/openapi/v2/alibaba/wan-3.0-prime/image-to-video",
        "query_endpoint": "/openapi/v2/query",
        "env_key": "RUNNINGHUB_API_KEY",
        "auth": "bearer",
        "label": "RunningHub 模型 API（企业级-共享 Key，扣团队/企业余额）",
    },
    "runninghub_workflow": {
        "dialect": "runninghub_workflow",
        "base": "https://www.runninghub.ai",
        "create_endpoint": "/task/openapi/create",
        "status_endpoint": "/task/openapi/status",
        "outputs_endpoint": "/task/openapi/outputs",
        "env_key": "RUNNINGHUB_API_KEY",
        "auth": "body",
        "label": "RunningHub 工作流 API（会员 Key 亦可，扣个人钱包 / RH 币）",
    },
}

RETRYABLE = {429, 503, 504}
DONE_OK = {"succeed", "success", "succeeded", "completed", "done"}
DONE_FAIL = {"failed", "fail", "error", "cancelled", "canceled"}
QUEUE = {"inqueue", "in_progress", "processing", "pending", "running", "queued"}

MAX_API_RETRIES = 3
MAX_WAIT_SECONDS = 15 * 60
INITIAL_POLL_SECONDS = 5
MAX_POLL_SECONDS = 20

# RunningHub 业务错误码 -> 人话解释（实测整理，避免把资金问题误判成代码 bug）
RH_ERRORS = {
    "812": "企业级-共享 Key 绑定的团队/企业余额不足。注意：个人钱包里的 RH 币或会员权益不会自动给这个 Key 用；请到「任务与账单 → 钱包/充值」确认充值到了团队余额，或改用消费级-会员 Key 走工作流 API。",
    "806": "API Key 无效（APIKEY_USER_NOT_FOUND），请检查密钥是否复制完整、是否已被删除或重置。",
    "416": "账户余额不足，请充值后重试。",
    "1004": "任务不存在或已过期，请检查 taskId。",
    "1007": "请求参数不合法，按 errorMessage 里的 allowed values 修正。",
    "1014": "这把是消费级-会员 Key，标准模型 API 仅限企业级-共享 Key 调用。请改用 --provider runninghub_workflow 走工作流 API（会员 Key 可用），或换一把企业级-共享 Key。",
}


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
        log("  [retry %d] %s 秒后重试" % (attempt + 1, round(delay, 1)))
        time.sleep(delay)
    raise RuntimeError("unreachable")


def pick(d, keys):
    """从不同厂商的响应里兜底取字段（含嵌套 dict / list）。"""
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k]:
                return d[k]
        items = d.values()
    elif isinstance(d, list):
        items = d
    else:
        return None
    for v in items:
        if isinstance(v, (dict, list)):
            hit = pick(v, keys)
            if hit:
                return hit
    return None


def extract_video_url(payload):
    for keys in (("video_url",), ("url",), ("fileUrl",)):
        hit = pick(payload, keys)
        if isinstance(hit, str) and hit.startswith("http"):
            return hit
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                for k in ("url", "fileUrl"):
                    if isinstance(item.get(k), str) and item[k].startswith("http"):
                        return item[k]
    return None


def degraded(reason, extra=None):
    """显式降级：把「为什么没出视频」写进结果，绝不假装成功。"""
    out = {
        "status": "degraded",
        "degraded": True,
        "video_url": None,
        "reason": reason,
        "boundary": "视频渲染不属于端侧作业主体；本次未产生任何成功的云端推理。",
    }
    if extra:
        out.update(extra)
    return out


def rh_business_error(resp, stage):
    """RunningHub 用 HTTP 200 + errorCode 表达失败，必须单独识别。"""
    code = str(resp.get("errorCode", "") or "")
    if not code or code == "0":
        return None
    msg = resp.get("errorMessage", "") or ""
    return degraded(
        "RunningHub 在 %s 阶段返回业务错误 %s：%s。%s"
        % (stage, code, msg, RH_ERRORS.get(code, "请对照平台错误码文档排查。")),
        {"error_code": code, "stage": stage, "provider_error": msg},
    )


def save_video(url, path):
    """视频 URL 通常有时效，成功后立刻落盘。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return {"saved_to": os.path.abspath(path), "bytes": os.path.getsize(path)}
    except Exception as e:
        return {"save_error": str(e)}


def finish(result, save_path):
    if save_path and result.get("video_url"):
        result["download"] = save_video(result["video_url"], save_path)
        log("[4/4] 已下载 -> %s" % result["download"].get("saved_to", "下载失败"))
    return result


# --------------------------------------------------------------------------
# 通道 A：标准异步（submit + status，requestId）
# --------------------------------------------------------------------------
def run_standard(cfg, headers, prompt, model, image_url, started, save_path, wait):
    payload = {"model": model or cfg["default_model"], "prompt": prompt}
    if image_url:
        payload["image_url"] = image_url
    log("[1/3] submit -> %s" % cfg["submit_url"])
    try:
        submitted = request_with_retry(cfg["submit_url"], payload, headers)
    except Exception as e:
        return degraded("提交任务失败：%s" % e, {"stage": "submit"})

    request_id = pick(submitted, cfg["id_keys"])
    if not request_id:
        return degraded(
            "提交响应里没找到任务 ID（找过 %s）。" % ", ".join(cfg["id_keys"]),
            {"stage": "submit", "raw": json.dumps(submitted, ensure_ascii=False)[:500]},
        )
    log("  requestId = %s" % request_id)
    if not wait:
        return {"status": "queued", "degraded": False, "request_id": request_id,
                "video_url": None, "reason": "wait=false：只提交，交给队列状态机续查。"}

    log("[2/3] poll（间隔递增，最长 %d 分钟）" % (MAX_WAIT_SECONDS // 60))
    deadline, interval, last_state = time.monotonic() + MAX_WAIT_SECONDS, INITIAL_POLL_SECONDS, "unknown"
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            st = request_with_retry(cfg["status_url"], {"requestId": request_id}, headers)
        except Exception as e:
            return degraded("轮询失败：%s" % e, {"stage": "poll", "request_id": request_id})
        last_state = str(pick(st, cfg["status_keys"]) or "unknown").lower()
        log("  state = %s" % last_state)
        if last_state in DONE_OK:
            return finish({
                "status": "succeeded", "degraded": False, "request_id": request_id,
                "video_url": extract_video_url(st),
                "elapsed_s": round(time.time() - started, 1), "reason": None,
            }, save_path)
        if last_state in DONE_FAIL:
            return degraded("云端任务失败：%s" % last_state,
                            {"stage": "poll", "request_id": request_id})
        interval = min(int(interval * 1.5) + 1, MAX_POLL_SECONDS)
    return {"status": "poll_timeout", "degraded": False, "request_id": request_id,
            "video_url": None, "last_state": last_state,
            "reason": "本地等待上限已到，任务可能仍在队列；请用同一 ID 续查，不要重复提交（会重复扣费）。"}


# --------------------------------------------------------------------------
# 通道 B：RunningHub 模型 API（Bearer + /openapi/v2/... + /openapi/v2/query）
# --------------------------------------------------------------------------
def run_runninghub_v2(cfg, headers, prompt, image_url, endpoint, base,
                      duration, resolution, aspect_ratio, started, save_path, wait):
    ep = endpoint or (cfg["image_endpoint"] if image_url else cfg["default_endpoint"])
    url = base.rstrip("/") + ep
    payload = {
        "prompt": prompt,
        "resolution": resolution,
        "aspectRatio": aspect_ratio,
        "duration": str(duration),
        "audio": False,
    }
    if image_url:
        payload["firstFrameUrl"] = image_url
    log("[1/3] submit -> %s" % url)
    try:
        submitted = request_with_retry(url, payload, headers)
    except Exception as e:
        return degraded("提交任务失败：%s" % e, {"stage": "submit"})

    err = rh_business_error(submitted, "submit")
    if err:
        return err
    task_id = submitted.get("taskId") or pick(submitted, ("taskId", "task_id"))
    if not task_id:
        return degraded("提交成功但没拿到 taskId，请对照平台文档核对响应字段。",
                        {"stage": "submit", "raw": json.dumps(submitted, ensure_ascii=False)[:500]})
    log("  taskId = %s" % task_id)
    if not wait:
        return {"status": "queued", "degraded": False, "request_id": task_id,
                "video_url": None, "reason": "wait=false：只提交，稍后用 /openapi/v2/query 续查。"}

    query_url = base.rstrip("/") + cfg["query_endpoint"]
    log("[2/3] poll /openapi/v2/query（间隔递增，最长 %d 分钟）" % (MAX_WAIT_SECONDS // 60))
    deadline, interval = time.monotonic() + MAX_WAIT_SECONDS, INITIAL_POLL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            st = request_with_retry(query_url, {"taskId": task_id}, headers)
        except Exception as e:
            return degraded("轮询失败：%s" % e, {"stage": "poll", "request_id": task_id})
        err = rh_business_error(st, "poll")
        if err:
            return err
        state = str(st.get("status", "unknown")).lower()
        log("  state = %s" % state)
        if state in DONE_OK:
            log("[3/3] 完成，耗时 %.1fs" % (time.time() - started))
            return finish({
                "status": "succeeded", "degraded": False, "request_id": task_id,
                "video_url": extract_video_url(st),
                "elapsed_s": round(time.time() - started, 1),
                "usage": st.get("usage"), "reason": None,
            }, save_path)
        if state in DONE_FAIL:
            return degraded("云端任务失败：%s | %s" % (state, st.get("errorMessage", "")),
                            {"stage": "poll", "request_id": task_id,
                             "failed_reason": st.get("failedReason")})
        interval = min(int(interval * 1.5) + 1, MAX_POLL_SECONDS)
    return {"status": "poll_timeout", "degraded": False, "request_id": task_id,
            "video_url": None, "last_state": "unknown",
            "reason": "本地等待上限已到，任务可能仍在队列；请用同一 taskId 续查，不要重复提交。"}


# --------------------------------------------------------------------------
# 通道 C：RunningHub 工作流 API（body 传 apiKey + workflowId）
# --------------------------------------------------------------------------
def run_runninghub_workflow(cfg, key, prompt, workflow_id, image_url, base,
                            started, save_path, wait, node_info=None, task_id=None):
    if not workflow_id and not task_id:
        return degraded(
            "工作流 API 需要 workflowId（或 --task-id 续查已有任务）：在 RunningHub 打开目标工作流，"
            "URL 里那串数字就是 ID。也可改用 --provider runninghub 走模型 API（不需要 workflowId）。",
            {"stage": "config", "how_to_get": "https://www.runninghub.ai/#/workflow/<WORKFLOW_ID>"},
        )
    if task_id:
        log("[1/3] 复用已有 taskId = %s（不重新提交、不重复扣费）" % task_id)
    else:
        node_info = node_info or []
        create_payload = {"apiKey": key, "workflowId": workflow_id}
        if node_info:
            create_payload["nodeInfoList"] = node_info
        url = base.rstrip("/") + cfg["create_endpoint"]
        log("[1/3] create -> %s" % url)
        try:
            created = request_with_retry(url, create_payload, {"Content-Type": "application/json"})
        except Exception as e:
            return degraded("创建工作流任务失败：%s" % e, {"stage": "create"})
        if created.get("code") not in (0, None):
            return degraded("创建任务被拒：code=%s msg=%s" % (created.get("code"), created.get("msg")),
                            {"stage": "create", "raw": json.dumps(created, ensure_ascii=False)[:400]})
        data = created.get("data") or {}
        task_id = data.get("taskId") if isinstance(data, dict) else None
        if not task_id:
            return degraded("创建响应里没拿到 taskId，请核对工作流是否已发布 / apiKey 类型是否匹配。",
                            {"stage": "create", "raw": json.dumps(created, ensure_ascii=False)[:400]})
        log("  taskId = %s" % task_id)
    if not wait:
        return {"status": "queued", "degraded": False, "request_id": task_id, "video_url": None}

    status_url = base.rstrip("/") + cfg["status_endpoint"]
    deadline, interval = time.monotonic() + MAX_WAIT_SECONDS, INITIAL_POLL_SECONDS
    log("[2/3] poll /task/openapi/status")
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            st = request_with_retry(status_url, {"apiKey": key, "taskId": task_id},
                                    {"Content-Type": "application/json"})
        except Exception as e:
            return degraded("轮询失败：%s" % e, {"stage": "poll", "request_id": task_id})
        raw = st.get("data")
        if isinstance(raw, str):
            state, sd = raw.lower(), {}
        elif isinstance(raw, dict):
            sd = raw
            state = str(sd.get("status") or st.get("status") or "unknown").lower()
        else:
            sd = {}
            state = str(st.get("status") or "unknown").lower()
        log("  state = %s" % state)
        if state in DONE_OK:
            try:
                out = request_with_retry(base.rstrip("/") + cfg["outputs_endpoint"],
                                         {"apiKey": key, "taskId": task_id},
                                         {"Content-Type": "application/json"})
            except Exception as e:
                return degraded("取输出失败：%s" % e, {"stage": "outputs", "request_id": task_id})
            return finish({
                "status": "succeeded", "degraded": False, "request_id": task_id,
                "video_url": extract_video_url(out),
                "elapsed_s": round(time.time() - started, 1), "reason": None,
            }, save_path)
        if state in DONE_FAIL:
            return degraded("工作流任务失败：%s | %s" % (state, sd.get("msg") if isinstance(sd, dict) else ""),
                            {"stage": "poll", "request_id": task_id})
        interval = min(int(interval * 1.5) + 1, MAX_POLL_SECONDS)
    return {"status": "poll_timeout", "degraded": False, "request_id": task_id, "video_url": None,
            "reason": "本地等待上限已到，请用同一 taskId 续查，不要重复提交。"}


def generate(prompt, provider="siliconflow", model=None, image_url=None,
             api_key=None, wait=True, mock=False, endpoint=None, workflow_id=None,
             duration="4", resolution="480P", aspect_ratio="16:9",
             base=None, save_path=None, node_info=None, task_id=None):
    """返回结构化结果；调用方可直接把它塞进 Dify 的结束节点。"""
    started = time.time()
    if mock:
        return degraded("已强制 --mock：演示降级路径，未发出任何外部请求。",
                        {"would_submit": {"prompt": prompt[:120], "provider": provider}})

    cfg = PROVIDERS.get(provider)
    if not cfg:
        return degraded("未登记的供应商 %r，可选：%s" % (provider, ", ".join(PROVIDERS)))

    key = api_key or os.environ.get(cfg["env_key"], "").strip()
    if not key:
        return degraded(
            "未配置 %s，适配器按红线保持端侧降级，不调用云端视频服务。" % cfg["env_key"],
            {"provider": cfg["label"], "how_to_enable": "export %s=你的密钥" % cfg["env_key"]},
        )

    headers = {"Content-Type": "application/json"}
    if cfg["auth"] == "bearer":
        headers["Authorization"] = "Bearer " + key
    host = base or cfg.get("base", "")

    if cfg["dialect"] == "runninghub_v2":
        return run_runninghub_v2(cfg, headers, prompt, image_url, endpoint, host,
                                 duration, resolution, aspect_ratio, started, save_path, wait)
    if cfg["dialect"] == "runninghub_workflow":
        return run_runninghub_workflow(cfg, key, prompt, workflow_id, image_url, host,
                                       started, save_path, wait, node_info, task_id)
    return run_standard(cfg, headers, prompt, model, image_url, started, save_path, wait)


class Handler(BaseHTTPRequestHandler):
    """浏览器工作台直连入口：CORS 预检 + JSON 回包（含显式降级原因）。

    页面从 file:// 或 https:// 打开时，POST application/json 会先发 OPTIONS
    预检；没有 do_OPTIONS 会被浏览器直接拦下，页面表现为「服务明明开着却调不通」。
    """

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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") not in ("/api/video/generate", "/api/video"):
            self._json({"error": "unknown path", "path": self.path}, 404)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            inp = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            inp = {}
        result = generate(
            prompt=inp.get("prompt") or inp.get("script") or "",
            provider=inp.get("provider", "siliconflow"),
            model=inp.get("model"), image_url=inp.get("image_url"),
            api_key=inp.get("api_key"), wait=inp.get("wait", True),
            mock=bool(inp.get("mock")) or os.environ.get("VIDEO_ADAPTER_MOCK") == "1",
            endpoint=inp.get("endpoint"), workflow_id=inp.get("workflow_id"),
            duration=inp.get("duration", "4"), resolution=inp.get("resolution", "480P"),
            aspect_ratio=inp.get("aspect_ratio", "16:9"), base=inp.get("base"),
            node_info=inp.get("node_info"), task_id=inp.get("task_id"),
        )
        self._json(result)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({
                "ok": True, "providers": list(PROVIDERS),
                "keys_configured": {k: bool(os.environ.get(v["env_key"]))
                                    for k, v in PROVIDERS.items()},
                "endpoints": ["GET /health", "POST /api/video/generate"],
            })
        else:
            self._json({"error": "unknown path", "path": self.path}, 404)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="视频生成 API 异步适配器（演示增强）")
    ap.add_argument("--prompt", default="一只穿黄色雨衣的柯基叼着棉花糖飘过雨后天台")
    ap.add_argument("--image-url", default=None, help="首帧图 URL（图生视频）")
    ap.add_argument("--provider", default="siliconflow", choices=list(PROVIDERS))
    ap.add_argument("--model", default=None, help="模型名（standard 通道用）")
    ap.add_argument("--endpoint", default=None, help="自定义 v2 模型端点路径")
    ap.add_argument("--workflow-id", default=None, help="RunningHub 工作流 ID")
    ap.add_argument("--base", default=None, help="自定义域名，如 https://www.runninghub.cn")
    ap.add_argument("--duration", default="4", help="秒数，以端点允许的枚举为准")
    ap.add_argument("--resolution", default="480P")
    ap.add_argument("--aspect-ratio", default="16:9")
    ap.add_argument("--node-info", default=None, help="工作流节点覆盖参数的 JSON 字符串")
    ap.add_argument("--task-id", default=None, help="续查已有任务，不重新提交（避免重复扣费）")
    ap.add_argument("--save", default=None, help="成功后把 MP4 下载到该路径")
    ap.add_argument("--mock", action="store_true", help="强制显式降级，不发外部请求")
    ap.add_argument("--no-wait", action="store_true", help="只提交任务，不轮询")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT", help="起 HTTP 服务")
    args = ap.parse_args()

    if args.serve:
        log("视频适配器已监听 http://127.0.0.1:%d" % args.serve)
        log("POST /api/video/generate | GET /health")
        log("已配置的 Key：%s" % ", ".join(k for k, v in PROVIDERS.items()
                                          if os.environ.get(v["env_key"])) or "无（将显式降级）")
        srv = ThreadingHTTPServer(("127.0.0.1", args.serve), Handler)
        srv.daemon_threads = True
        srv.serve_forever()
        return

    result = generate(
        prompt=args.prompt, provider=args.provider, model=args.model,
        image_url=args.image_url, wait=not args.no_wait, mock=args.mock,
        endpoint=args.endpoint, workflow_id=args.workflow_id,
        duration=args.duration, resolution=args.resolution,
        aspect_ratio=args.aspect_ratio, base=args.base, save_path=args.save,
        node_info=json.loads(args.node_info) if args.node_info else None,
        task_id=args.task_id,
    )
    print("\n--- 结构化结果 ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("succeeded", "queued") else 1


if __name__ == "__main__":
    sys.exit(main())
