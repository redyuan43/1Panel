from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from .contracts import VIDEO_ASPECT_RATIOS, VIDEO_CREATIVE_PROFILES, VIDEO_WORKFLOW_MODES


WORKFLOW_DESCRIPTORS = (
    {
        "id": "quality_gate",
        "label": "方案 - 预览 - 成片",
        "description": "先确认提示词、分镜和锚点，再确认低成本预览，最后确认高质量成片。",
        "stages": ("plan", "preview", "final"),
    },
    {
        "id": "duration_ladder",
        "label": "约 5 秒 - 约 10 秒 - 约 15 秒",
        "description": "每次批准后追加一个片段，不重新生成已经批准的内容。",
        "stages": ("clip_5s", "clip_10s", "clip_15s"),
    },
    {
        "id": "legacy_pipeline",
        "label": "原始 H3 流程",
        "description": "保留 Context IR、预览、质量验证、768P 和 2K 阶段。",
        "stages": (),
    },
)

STAGE_LABELS = {
    "plan": "方案确认",
    "preview": "预览确认",
    "final": "成片确认",
    "clip_5s": "约 5 秒",
    "clip_10s": "约 10 秒",
    "clip_15s": "约 15 秒",
}

SEGMENT_BLOCKERS = {
    "continuous_dialogue": (
        r"\b(dialogue|conversation|speaks?|talks?|lip[- ]?sync|monologue)\b",
        r"对白|对话|说话|讲话|口型|唇形|独白",
    ),
    "continuous_camera": (
        r"\b(one[- ]?take|single continuous shot|continuous (?:camera|tracking|dolly|orbit))\b",
        r"一镜到底|单一连续镜头|连续运镜|持续跟拍|持续环绕",
    ),
    "fast_cross_boundary_action": (
        r"\b(fast action|fight|chase|handoff|throws?|catches?|continuous transformation)\b",
        r"快速动作|打斗|追逐|交接|递给|抛出|接住|连续变形|连续换装",
    ),
    "complex_physics": (
        r"\b(fluid|splash|smoke simulation|particles?|cloth simulation|explosion)\b",
        r"流体|飞溅|烟雾模拟|粒子|布料模拟|爆炸",
    ),
    "strict_music_timing": (
        r"\b(on the beat|beat[- ]?synced|strict rhythm|music video lip sync)\b",
        r"卡点|踩点|严格节拍|音乐同步|对口型",
    ),
}

NEGATED_BLOCKER_PATTERNS = (
    r"\b(?:no|without)\s+(?:continuous\s+)?(?:dialogue|conversation|speech|talking|lip[- ]?sync)\b",
    r"\b(?:no|without)\s+(?:object\s+)?(?:handoff|hand-off|throwing|catching)\b",
    r"\b(?:no|without)\s+(?:fast\s+)?(?:action|fight|chase)\b",
    r"\b(?:no|without)\s+(?:strict\s+)?(?:music timing|rhythm|beat sync)\b",
    r"无(?:连续)?(?:对白|对话|说话|讲话|口型|唇形)",
    r"(?:没有|不含|不要)(?:连续)?(?:对白|对话|说话|讲话|口型|唇形)",
    r"无(?:物体)?(?:交接|抛出|接住)",
)


@dataclass(frozen=True)
class SegmentDecision:
    eligible: bool
    reasons: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


@lru_cache(maxsize=1)
def prompt_profiles() -> dict:
    path = Path(__file__).with_name("prompt_profiles.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("policy", {}).get("external_api_dependency") is not False:
        raise ValueError("invalid local video prompt profile set")
    return value


def workflow_mode(request: dict) -> str:
    value = request.get("workflow_mode", "legacy_pipeline")
    return value if value in VIDEO_WORKFLOW_MODES else "legacy_pipeline"


def managed_stages(request: dict) -> list[dict]:
    mode = workflow_mode(request)
    descriptor = next(item for item in WORKFLOW_DESCRIPTORS if item["id"] == mode)
    return [
        {
            "id": stage_id,
            "label": STAGE_LABELS[stage_id],
            "status": "queued" if index == 0 else "pending",
            "progress": 1 if index == 0 else 0,
            "run_id": None,
            "output_id": None,
        }
        for index, stage_id in enumerate(descriptor["stages"])
    ]


def segmentation_blockers(request: dict) -> tuple[str, ...]:
    reasons = []
    if request.get("audio_policy") == "lock_source":
        reasons.append("locked_continuous_audio")
    text = str(request.get("prompt", "")).lower()
    for pattern in NEGATED_BLOCKER_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    for reason, patterns in SEGMENT_BLOCKERS.items():
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            reasons.append(reason)
    return tuple(reasons)


def segmentation_enabled() -> bool:
    return os.environ.get("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def classify_segmentation(request: dict) -> SegmentDecision:
    mode = workflow_mode(request)
    if mode not in {"quality_gate", "duration_ladder"}:
        return SegmentDecision(False, ("workflow_not_quality_gate",))
    if not segmentation_enabled():
        return SegmentDecision(False, ("parallel_segmentation_shelved",))
    if int(request.get("duration", 0)) != 15:
        return SegmentDecision(False, ("duration_not_15_seconds",))
    reasons = segmentation_blockers(request)
    if reasons:
        return SegmentDecision(False, reasons)
    return SegmentDecision(
        True,
        (
            "duration_ladder_incremental_segments"
            if mode == "duration_ladder"
            else "three_stable_five_second_beats",
        ),
    )


def creative_profile(request: dict) -> str:
    selected = request.get("creative_profile", "auto")
    if selected != "auto":
        return selected
    text = str(request.get("prompt", "")).lower()
    matches = (
        ("ecommerce", ("product", "shopping", "商品", "产品", "电商")),
        ("social_commerce", ("social commerce", "livestream", "带货", "直播")),
        ("short_drama", ("short drama", "剧情", "短剧")),
        ("dynamic_comic", ("comic", "anime", "漫画", "动画")),
        ("tvc", ("tvc", "television commercial", "电视广告")),
        ("ai_ad", ("advertisement", "campaign", "广告", "品牌片")),
        ("seeding", ("recommendation", "种草", "测评")),
    )
    return next((profile for profile, words in matches if any(word in text for word in words)), "general")


def prompt_package_hash(package: dict) -> str:
    value = {key: item for key, item in package.items() if key != "prompt_hash"}
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def refresh_prompt_hash(package: dict) -> dict:
    value = dict(package)
    value["prompt_hash"] = prompt_package_hash(value)
    return value


def build_prompt_package(request: dict) -> dict:
    profiles = prompt_profiles()
    categories = profiles["categories"]
    decision = classify_segmentation(request)
    duration = int(request["duration"])
    ratio = request.get("aspect_ratio", "16:9")
    segments = []
    if decision.eligible:
        for index, (start, end) in enumerate(((0, 5), (5, 10), (10, 15)), 1):
            segments.append(
                {
                    "id": f"segment_{index}",
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": 5,
                    "first_anchor_seconds": start,
                    "last_anchor_seconds": end,
                    "prompt": (
                        f"Global continuity is mandatory. Segment {index} of 3, {start}-{end} seconds. "
                        f"Preserve identity, wardrobe state, props, carriage geometry, lighting and camera axis. "
                        f"Primary request: {request['prompt']}"
                    ),
                }
            )
    else:
        segments.append(
            {
                "id": "segment_1",
                "start_seconds": 0,
                "end_seconds": duration,
                "duration_seconds": duration,
                "first_anchor_seconds": 0,
                "last_anchor_seconds": duration,
                "prompt": request["prompt"],
            }
        )
    package = {
        "schema_version": 1,
        "workflow_mode": workflow_mode(request),
        "creative_profile": creative_profile(request),
        "mode": request["mode"],
        "duration_seconds": duration,
        "fps": 24,
        "aspect_ratio": ratio if ratio in VIDEO_ASPECT_RATIOS else "16:9",
        "ruleset": {
            "profile_set": profiles["profile_set"],
            "sha256": sha256(
                json.dumps(profiles, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
        "global_constraints": {
            "h3": [item["requirement"] for item in categories["h3"]["rules"]],
            "cinematography": [item["requirement"] for item in categories["cinematography"]["rules"]],
            "first_last_frame": categories["first_last_frame"]["approval_requirements"],
            "commercial": categories["commercial"]["global_rules"],
            "quality_feedback": [item["requirement"] for item in categories["quality_feedback"]["rules"]],
        },
        "continuity_bible": {
            "identity": "Preserve the approved adult subject's face, hair, body proportions and skin tone.",
            "wardrobe": "Carry the exact approved clothing state across every shared boundary.",
            "props": "Preserve object identity, text placement, handedness and ownership.",
            "environment": "Preserve layout, camera axis, lighting direction and color response.",
            "camera": "Use the approved framing and avoid an unplanned cut at a segment boundary.",
            "sound": "Preserve ambience, voice identity and timing according to the selected audio policy.",
        },
        "segmentation": decision.public(),
        "split_decision": decision.public(),
        "segments": segments,
        "storyboard": [
            {
                "segment_id": segment["id"],
                "time_range_seconds": [
                    segment["start_seconds"],
                    segment["end_seconds"],
                ],
                "start_state": f"Approved anchor T{segment['first_anchor_seconds']}",
                "end_state": f"Approved anchor T{segment['last_anchor_seconds']}",
                "action": segment["prompt"],
            }
            for segment in segments
        ],
        "audio_plan": {
            "policy": request["audio_policy"],
            "reference_audio": "reference_audio" in request["assets"],
            "continuous_source_track": request["audio_policy"] in {"reference", "lock_source"},
            "native_ambience_crossfade_seconds": 0.08,
            "force_single_segment": request["audio_policy"] == "lock_source",
        },
        "anchor_seconds": [0, 5, 10, 15] if decision.eligible else [0, duration],
    }
    return refresh_prompt_hash(package)


def options() -> dict[str, Any]:
    split_enabled = segmentation_enabled()
    descriptors = [
        item
        for item in WORKFLOW_DESCRIPTORS
        if item["id"] != "duration_ladder" or split_enabled
    ]
    return {
        "workflow_mode": [item["id"] for item in descriptors],
        "workflow_modes": [dict(item) for item in descriptors],
        "default_workflow_mode": "quality_gate",
        "creative_profile": list(VIDEO_CREATIVE_PROFILES),
        "aspect_ratio": list(VIDEO_ASPECT_RATIOS),
        "review_policy": "advisory",
        "segmentation_policy": "conditional_auto" if split_enabled else "disabled",
        "duration_ladder_available": split_enabled,
        "duration_ladder_requires_segmentable_prompt": split_enabled,
    }
