#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
出图引擎驱动层（零第三方依赖，只用 Python 标准库）

三种驱动，由 local_api_server.py 的 --engine 显式选择，默认 mock：

  mock   作业正式方案。只回「已排队」收据，不加载权重、不发任何外网请求、不产出像素。
         对应题目硬约束「全部推理在本地完成，无云端兜底」。

  local  真机方案。把任务提交给本机 / 局域网的 ComfyUI（POST /prompt），
         轮询 /history，再从 /view 取回图片。需要用户先导出 workflow_api.json。
         全部推理仍在本地，不联网。

  cloud  【DEV ONLY · 不属于作业正式方案】给没有显卡的机器做开发 / 课堂演示用：
         显式传 --engine cloud 才会启用，默认关闭。
         provider=pollinations 零注册零 key；provider=siliconflow 需 SILICONFLOW_API_KEY。

重要边界：cloud 是「显式切换的主引擎」，不是「本地失败后的云端兜底」。
任何驱动失败都只在本地报错并降级（降分辨率 / 减少候选 / 排队 / 拒绝），
绝不出现自动转云端的分支。cloud_fallback 字段恒为 False。

隐私提醒：cloud 驱动会把提示词发给第三方服务。演示请用虚构商品，
不要上传真实客户的商品图、LOGO 或未发布素材。
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_DIR = os.path.join(HERE, "outputs")


class EngineError(Exception):
    """驱动不可用或出图失败。上层只做本地降级，绝不转云端重试。"""


def http(url, data=None, headers=None, timeout=60, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    req.add_header("User-Agent", "AIStudio-Landing-Demo/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", ""), r.status
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        if e.code == 402 and "FREE_LIMIT" in detail:
            raise EngineError("匿名免费额度用尽（每 IP 每 45 秒 1 张）：稍等再点即可；"
                              "或设置 POLLINATIONS_API_KEY 走无限流通道（enter.pollinations.ai）")
        raise EngineError("HTTP %s：%s" % (e.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EngineError("网络不可达或超时：%s" % e)


# --------------------------------------------------------------------------
# 驱动 1：Pollinations（零注册、零 API key，联网即可出真图）
# --------------------------------------------------------------------------

def render_pollinations(prompt, seed, size, timeout=60):
    """
    两条通道：
      匿名（默认）：classic 端点 image.pollinations.ai/prompt/...，零 key，
                    但每 IP 每 45 秒 1 张，适合课堂单次演示。
      带 key：新版网关 gen.pollinations.ai/image/... + Bearer，无速率限制。
              在 enter.pollinations.ai 申请 sk_ 开头的密钥，设进 POLLINATIONS_API_KEY。
    """
    key = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    enc = urllib.parse.quote(prompt, safe="")
    if key:
        base = os.environ.get("POLLINATIONS_BASE", "https://gen.pollinations.ai").rstrip("/")
        model = os.environ.get("POLLINATIONS_MODEL", "black-forest-labs/flux.1-schnell")
        qs = urllib.parse.urlencode({"width": size, "height": size, "seed": seed,
                                     "model": model, "nologo": "true"})
        raw, ctype, _ = http(base + "/image/" + enc + "?" + qs,
                             headers={"Authorization": "Bearer " + key}, timeout=timeout)
    else:
        base = os.environ.get("POLLINATIONS_CLASSIC_BASE", "https://image.pollinations.ai").rstrip("/")
        qs = urllib.parse.urlencode({"width": size, "height": size, "seed": seed,
                                     "model": os.environ.get("POLLINATIONS_MODEL_CLASSIC", "flux"),
                                     "nologo": "true"})
        raw, ctype, _ = http(base + "/prompt/" + enc + "?" + qs, timeout=timeout)
    if "image" not in ctype.lower():
        raise EngineError("返回的不是图片（Content-Type=%s）" % ctype)
    return raw, "image/jpeg"


# --------------------------------------------------------------------------
# 驱动 2：硅基流动（OpenAI 兼容形态，需要 API key）
# --------------------------------------------------------------------------

def render_siliconflow(prompt, seed, size, timeout=90):
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        raise EngineError("缺少环境变量 SILICONFLOW_API_KEY（在 cloud.siliconflow.cn 控制台创建）")
    model = os.environ.get("SILICONFLOW_MODEL", "Kwai-Kolors/Kolors")
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": "%dx%d" % (size, size),
        "num_inference_steps": 20,
        "seed": seed % 9999999999,
    }
    raw, _, _ = http(
        "https://api.siliconflow.cn/v1/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key,
                 "X-Enable-Watermark": "0"},
        timeout=timeout,
    )
    try:
        j = json.loads(raw.decode("utf-8"))
        img_url = j["images"][0]["url"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise EngineError("回包里没有 images[0].url：%s" % raw[:200].decode("utf-8", "replace"))
    # 官方说明：图片 URL 有效期约 1 小时，必须立刻下载落盘
    img_raw, _, _ = http(img_url, timeout=timeout)
    return img_raw, "image/jpeg"


# --------------------------------------------------------------------------
# 驱动 3：本机 ComfyUI（真正的端侧落地，对应文档①的运行框架）
# --------------------------------------------------------------------------

def render_comfyui(prompt, seed, size, timeout=180):
    base = os.environ.get("COMFYUI_BASE", "http://127.0.0.1:8188").rstrip("/")
    wf_path = os.environ.get("COMFYUI_WORKFLOW_JSON") or os.path.join(HERE, "workflow_api.json")
    if not os.path.exists(wf_path):
        raise EngineError("缺少 %s：请在 ComfyUI 里把工作流导出为 workflow_api.json 放到本目录" % wf_path)
    try:
        with open(wf_path, encoding="utf-8") as f:
            wf = json.load(f)
    except (OSError, ValueError) as e:
        raise EngineError("workflow_api.json 读取失败：%s" % e)

    text_node = os.environ.get("COMFYUI_PROMPT_NODE_ID")
    text_nodes = [k for k, v in wf.items()
                  if isinstance(v, dict) and v.get("class_type") == "CLIPTextEncode"]
    if not text_nodes:
        raise EngineError("工作流里没有 CLIPTextEncode 节点，无法注入提示词")
    wf[text_node or text_nodes[0]]["inputs"]["text"] = prompt
    for k, v in wf.items():
        if isinstance(v, dict) and v.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            if "seed" in v.get("inputs", {}):
                v["inputs"]["seed"] = seed % (2 ** 31)
    # 尺寸节点（若存在 EmptyLatentImage / EmptySD3LatentImage）
    for k, v in wf.items():
        if isinstance(v, dict) and v.get("class_type") in ("EmptyLatentImage", "EmptySD3LatentImage"):
            v["inputs"]["width"] = size
            v["inputs"]["height"] = size

    raw, _, _ = http(base + "/prompt",
                     data=json.dumps({"prompt": wf}, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"}, timeout=30)
    try:
        pid = json.loads(raw.decode("utf-8"))["prompt_id"]
    except (ValueError, KeyError):
        raise EngineError("ComfyUI 没返回 prompt_id：%s" % raw[:200].decode("utf-8", "replace"))

    deadline = time.time() + timeout
    while time.time() < deadline:
        hraw, _, _ = http(base + "/history/" + pid, timeout=15)
        try:
            hist = json.loads(hraw.decode("utf-8"))
        except ValueError:
            hist = {}
        if pid in hist:
            for node_out in (hist[pid].get("outputs") or {}).values():
                for im in (node_out.get("images") or []):
                    qs = urllib.parse.urlencode({
                        "filename": im.get("filename", ""),
                        "subfolder": im.get("subfolder", ""),
                        "type": im.get("type", "output"),
                    })
                    img_raw, ctype, _ = http(base + "/view?" + qs, timeout=30)
                    mime = ctype.split(";")[0] or "image/png"
                    return img_raw, mime
        time.sleep(2)
    raise EngineError("ComfyUI 在 %ds 内没有出图，请检查队列或显存" % timeout)


DRIVERS = {
    "pollinations": render_pollinations,
    "siliconflow": render_siliconflow,
    "comfyui": render_comfyui,
}


def _safe(s):
    return "".join(c for c in str(s) if c.isalnum() or c in "-_")[:20] or "item"


def image_name(scene, sku, tag, ext):
    return "%s_%s_%s%s" % (scene, _safe(sku), tag, ext)


def cache_tag(engine, provider, scene, sku, prompt, seed, size):
    """同参数同结果 → 命中本地缓存，不重复消耗匿名免费额度。"""
    raw = "|".join(map(str, [engine, provider, scene, sku, prompt, seed, size]))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def save_image(raw, mime, scene, sku, output_dir, tag):
    os.makedirs(output_dir, exist_ok=True)
    ext = ".png" if "png" in mime else ".jpg"
    name = image_name(scene, sku, tag, ext)
    path = os.path.join(output_dir, name)
    with open(path, "wb") as f:
        f.write(raw)
    return name, path, len(raw)


def render_one(engine, provider, scene, sku, prompt, seed, size, output_dir, host, port):
    """出一张图并落盘；命中缓存则直接复用。失败抛 EngineError。"""
    if engine == "mock":
        raise EngineError("mock 模式按设计不产出像素")
    fn = {"local": "comfyui", "cloud": provider}.get(engine)
    if fn not in DRIVERS:
        raise EngineError("未知驱动：%s" % fn)
    tag = cache_tag(engine, fn, scene, sku, prompt, seed, size)

    # 内容寻址缓存：完全相同的任务不重复请求外部服务
    for ext in (".jpg", ".png"):
        hit = os.path.join(output_dir, image_name(scene, sku, tag, ext))
        if os.path.isfile(hit) and os.path.getsize(hit) > 1024:
            return {
                "url": "http://%s:%d/outputs/%s" % (host, port, os.path.basename(hit)),
                "file": os.path.basename(hit), "local_path": hit,
                "bytes": os.path.getsize(hit), "scene": scene, "sku": sku,
                "prompt": prompt, "seed": seed, "size": size, "provider": fn,
                "engine_mode": engine, "elapsed": 0, "cached": True,
                "dev_only": engine == "cloud",
            }

    t0 = time.time()
    raw, mime = DRIVERS[fn](prompt, seed, size)
    name, path, nbytes = save_image(raw, mime, scene, sku, output_dir, tag)
    return {
        "url": "http://%s:%d/outputs/%s" % (host, port, name),
        "file": name,
        "local_path": path,
        "bytes": nbytes,
        "scene": scene,
        "sku": sku,
        "prompt": prompt,
        "seed": seed,
        "size": size,
        "provider": fn,
        "engine_mode": engine,
        "elapsed": round(time.time() - t0, 2),
        "cached": False,
        "dev_only": engine == "cloud",
    }


def reuse_latest(scene, output_dir, host, port):
    """
    外部出图失败（例如匿名额度冷却）时，复用本地已落盘的同场景候选。
    这是「本地恢复 / 已出候选保留」，不是云端兜底：不重试、不发新请求、不转服务商。
    """
    if not os.path.isdir(output_dir):
        return None
    fs = [f for f in os.listdir(output_dir)
          if f.startswith(scene + "_") and f.lower().endswith((".jpg", ".png"))]
    if not fs:
        return None
    fs.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
    name, path = fs[0], os.path.join(output_dir, fs[0])
    return {
        "url": "http://%s:%d/outputs/%s" % (host, port, name),
        "file": name, "local_path": path, "bytes": os.path.getsize(path),
        "scene": scene, "sku": "复用本地候选", "prompt": "（复用已落盘产物，未发起新请求）",
        "seed": 0, "size": 0, "provider": "local-cache", "engine_mode": "cloud",
        "elapsed": 0, "cached": True, "reused": True, "dev_only": True,
    }


def probe(engine, provider):
    """自检用：只检查前置条件，不真的出图。返回 (ok, 说明)。"""
    if engine == "mock":
        return True, "离线验证器：不发外网请求、不产出像素（作业正式方案）"
    if engine == "local":
        base = os.environ.get("COMFYUI_BASE", "http://127.0.0.1:8188").rstrip("/")
        wf = os.environ.get("COMFYUI_WORKFLOW_JSON") or os.path.join(HERE, "workflow_api.json")
        if not os.path.exists(wf):
            return False, "还缺 workflow_api.json（从 ComfyUI 导出后放到本目录）"
        try:
            http(base + "/system_stats", timeout=5)
            return True, "ComfyUI 可达：%s" % base
        except EngineError as e:
            return False, "ComfyUI 未启动或地址不对：%s" % e
    if provider == "siliconflow":
        if not os.environ.get("SILICONFLOW_API_KEY"):
            return False, "还缺 SILICONFLOW_API_KEY 环境变量"
        target = "https://api.siliconflow.cn/v1/models"
    else:
        # 用公开的模型列表端点探活：不需要鉴权，也不会消耗出图额度
        target = "https://gen.pollinations.ai/v1/models"
    try:
        http(target, timeout=8)
        extra = "（已配 key，无限流）" if os.environ.get("POLLINATIONS_API_KEY") else "（匿名通道 45 秒 1 张）"
        return True, "外网可达，%s 可调用%s" % (provider, extra)
    except EngineError as e:
        s = str(e)
        # 402 FREE_LIMIT 只说明额度受限，网络本身是通的
        if "免费额度用尽" in s or "HTTP 4" in s:
            return True, "外网可达；当前匿名额度冷却中，稍等即可继续出图"
        return False, "外网不可达：%s" % s
