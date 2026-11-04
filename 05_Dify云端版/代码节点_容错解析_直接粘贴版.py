# Dify 代码节点专用：全选复制，替换原有代码后保存并发布

import json
import re

def extract_plan(text):
    """容错解析 LLM 的结构化输出：兼容 Markdown 围栏、前后解释文字、尾随逗号。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        for cand in (s[i:j+1], re.sub(r",\s*([}\]])", r"\1", s[i:j+1])):
            try:
                return json.loads(cand)
            except Exception:
                pass
    return None


def main(llm_text, shot_count, resolution, anchor_lock):
    plan = extract_plan(llm_text)
    if plan is None:
        # 本地规则兜底，绝不调用云端模型重试
        n = int(shot_count or 6)
        plan = {
            "plan_id": "drama-fallback",
            "engine": "Z-Image-Turbo Q5_K_S",
            "boundary": "竖屏 9:16 / 单镜头 ≤5s / 图先行",
            "characters": ["林夏", "陈屿"],
            "shots": [
                {"scene": "中景", "action": "两人对视，情绪克制", "dur": 4,
                 "prompt": "cinematic still, two characters, anchored reference",
                 "camera": "固定", "identity_ref": "原始角色锚图"}
                for _ in range(n)
            ],
        }
    n = int(shot_count or len(plan.get("shots", [])) or 6)
    shots = plan.get("shots", [])[:n]
    for s in shots:
        # 端侧边界在代码里强制落地，不信任模型自觉
        s["dur"] = min(int(s.get("dur", 4) or 4), 5)
        s["identity_ref"] = "原始角色锚图"
        s.setdefault("scene", "中景")
        s.setdefault("action", "情绪延续")
        s.setdefault("prompt", "cinematic still, anchored reference")
        s.setdefault("camera", "固定")
        s["stage"] = "keyframe"   # 图先行：本轮只排队关键帧，不启动视频
    plan["shots"] = shots
    plan["boundary"] = "竖屏 9:16 / 单镜头 ≤5s / 图先行 / I2V 5 帧段（无云端兜底）"
    drift = "关闭" in (anchor_lock or "")
    request = {
        "plan_id": plan.get("plan_id", "drama-local"),
        "scene": "drama",
        "engine": "Z-Image-Turbo Q5_K_S",
        "resolution": resolution,
        "anchor_lock": not drift,
        "local_only": True,
        "stage": "keyframe",
        "tasks": shots,
    }
    return {
        "plan_json": json.dumps(plan, ensure_ascii=False),
        "request_json": json.dumps(request, ensure_ascii=False),
        "shot_count": len(shots),
        "summary": "本地编排完成：%d 个镜头，均 ≤5s，图先行排队关键帧，re-anchor=%s"
                   % (len(shots), "关" if drift else "开"),
    }