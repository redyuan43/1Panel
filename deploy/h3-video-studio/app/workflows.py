from __future__ import annotations

import copy
import json
from pathlib import Path


VALID_MODES = {"t2v", "i2v", "l2v", "fl2v", "reference", "hybrid"}
VALID_STRATEGIES = {"fast", "safe", "cloud"}
VALID_AUDIO_POLICIES = {"native", "reference", "lock_source"}

TURBO_TEMPLATE = "h3_t8_turbo4_dual_clock_full_int8_15s_official_context_ir_api.json"
QUALITY_TEMPLATE = "h3_quality_14step_15s_official_context_ir_api.json"
QUALITY_768_TEMPLATE = (
    "h3_quality_14step_full_int8_1344x768_15s_official_context_ir_20260810_2300_api.json"
)

COMMUNITY_TEMPLATES = {
    "i2v": "community_samples/01_eva_i2va_quality14_api.json",
    "fl2v": "community_samples/02_eva_fl2va_quality14_api.json",
    "l2v": "community_samples/05_eva_l2va_quality14_api.json",
    "reference_image": "community_samples/12_eva_ref2va_image_full_int8_quality14_api.json",
    "reference_image_audio": "community_samples/04_eva_ref2va_image_audio_quality14_api.json",
    "reference_video": "community_samples/06_eva_ref_video_quality14_api.json",
    "reference_video_audio": "community_samples/07_eva_ref_video_audio_quality14_api.json",
    "audio_lock": "community_samples/08_eva_i2va_audio_lock_quality14_api.json",
    "hybrid": "community_samples/11_eva_hybrid_full_int8_quality14_api.json",
}

FL2VA_MODEL = "minimax_h3_fl2va_int8_convrot.safetensors"
REF2VA_MODEL = "minimax_h3_ref2va_int8_convrot.safetensors"

STAGE_LABELS = {
    "context_ir": "H3 Context IR",
    "preview": "低清预览",
    "proof": "质量验证",
    "local_768": "本地 768P",
    "cloud_768": "官方 768P",
    "regenerate_2k": "官方 2K",
}


def frames_for_duration(duration: int) -> int:
    if duration < 4 or duration > 15:
        raise ValueError("时长必须在 4 到 15 秒之间。")
    valid = list(range(107, 363, 17))
    target = duration * 24
    return min(valid, key=lambda value: (abs(value - target), value))


def actual_duration(duration: int) -> float:
    return frames_for_duration(duration) / 24


def uses_turbo_preview(project: dict) -> bool:
    return (
        project["mode"] in {"t2v", "i2v", "l2v", "fl2v"}
        and project.get("audio_policy", "native") == "native"
    )


def pipeline_for(project: dict) -> list[dict]:
    strategy = project["strategy"]
    mode = project["mode"]
    audio_policy = project.get("audio_policy", "native")
    stages = ["context_ir", "preview"]
    if strategy == "safe" and uses_turbo_preview(project):
        stages.append("proof")
    if strategy == "cloud" and mode != "hybrid" and audio_policy != "lock_source":
        stages.append("cloud_768")
    else:
        stages.append("local_768")
    stages.append("regenerate_2k")
    return [
        {
            "id": stage,
            "label": "原始提示词确认" if stage == "context_ir" and project.get("prompt_processing") == "manual" else STAGE_LABELS[stage],
            "runtime": runtime_profile(project, stage),
            **project["stages"][stage],
        }
        for stage in stages
    ]


def runtime_profile(project: dict, stage: str) -> dict:
    ratio = frames_for_duration(int(project["duration"])) / 362
    if stage == "context_ir":
        if project.get("prompt_processing") == "manual":
            return _runtime(0, 0, runner="本机原文确认", billing="无 API 调用", basis="不改写提示词，等待人工确认")
        return _runtime(
            30,
            90,
            runner="MiniMax 云端",
            billing="API 调用",
            basis="真实请求通常在一分钟左右完成",
        )
    if stage == "preview":
        if uses_turbo_preview(project):
            return _scaled_runtime(
                360,
                480,
                ratio,
                runner="本地 H3 节点",
                billing="本地算力",
                basis="原Edge参考：15秒4步约6分38秒；当前节点耗时以任务记录为准",
            )
        return _scaled_runtime(
            840,
            1080,
            ratio,
            runner="本地 H3 节点",
            billing="本地算力",
            basis="原Edge参考：15秒480P约16分08秒；当前节点耗时以任务记录为准",
        )
    if stage == "proof":
        return _scaled_runtime(
            840,
            1080,
            ratio,
            runner="本地 H3 节点",
            billing="本地算力",
            basis="原Edge参考：15秒480P约16分08秒；当前节点耗时以任务记录为准",
        )
    if stage == "local_768":
        return _scaled_runtime(
            3600,
            4200,
            ratio,
            runner="本地 H3 节点",
            billing="本地算力",
            basis="原Edge参考：15秒768P约1小时05分；当前节点耗时以任务记录为准",
        )
    if stage == "cloud_768":
        return {
            "low_seconds": None,
            "high_seconds": None,
            "label": "官方队列动态",
            "runner": "MiniMax 云端",
            "billing": "消耗 API 额度",
            "basis": "耗时由官方排队和生成负载决定",
        }
    if stage == "regenerate_2k":
        return {
            "low_seconds": None,
            "high_seconds": None,
            "label": "官方队列动态",
            "runner": "MiniMax 云端",
            "billing": "消耗 API 额度",
            "basis": "上传768P后由官方完成2K再生成",
        }
    raise ValueError(f"未知阶段：{stage}")


def project_runtime_summary(project: dict) -> dict:
    profiles = [stage["runtime"] for stage in pipeline_for(project)]
    known = [item for item in profiles if item["low_seconds"] is not None]
    dynamic = sum(1 for item in profiles if item["low_seconds"] is None)
    return {
        "low_seconds": sum(int(item["low_seconds"]) for item in known),
        "high_seconds": sum(int(item["high_seconds"]) for item in known),
        "dynamic_cloud_stages": dynamic,
    }


def _scaled_runtime(
    low_seconds: int,
    high_seconds: int,
    ratio: float,
    *,
    runner: str,
    billing: str,
    basis: str,
) -> dict:
    low = max(30, int(round(low_seconds * ratio / 30) * 30))
    high = max(low, int(round(high_seconds * ratio / 30) * 30))
    return _runtime(
        low,
        high,
        runner=runner,
        billing=billing,
        basis=basis,
    )


def _runtime(
    low_seconds: int,
    high_seconds: int,
    *,
    runner: str,
    billing: str,
    basis: str,
) -> dict:
    return {
        "low_seconds": low_seconds,
        "high_seconds": high_seconds,
        "label": _format_range(low_seconds, high_seconds),
        "runner": runner,
        "billing": billing,
        "basis": basis,
    }


def _format_range(low_seconds: int, high_seconds: int) -> str:
    return f"{_format_duration(low_seconds)}–{_format_duration(high_seconds)}"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 180:
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes}分{remainder:02d}秒" if remainder else f"{minutes}分钟"
    hours, remainder = divmod(seconds, 3600)
    minutes = round(remainder / 60)
    if hours:
        return f"{hours}小时{minutes:02d}分" if minutes else f"{hours}小时"
    return f"{max(1, minutes)}分钟"


def validate_project_config(project: dict) -> None:
    mode = project.get("mode")
    strategy = project.get("strategy")
    audio_policy = project.get("audio_policy")
    assets = project.get("assets", {})
    if project.get("orientation", "landscape") not in {"landscape", "portrait"}:
        raise ValueError("未知画面方向。")
    if project.get("prompt_processing", "cloud") not in {"cloud", "manual"}:
        raise ValueError("未知提示词处理方式。")
    if project.get("orientation") == "portrait" and strategy == "cloud":
        raise ValueError("竖版当前仅接入本地生成，请选择快速或稳妥策略。")
    if mode not in VALID_MODES:
        raise ValueError("未知生成模式。")
    if strategy not in VALID_STRATEGIES:
        raise ValueError("未知生产策略。")
    if audio_policy not in VALID_AUDIO_POLICIES:
        raise ValueError("未知音频策略。")
    frames_for_duration(int(project["duration"]))
    if not project.get("prompt_original", "").strip():
        raise ValueError("请输入原始提示词。")
    if mode == "i2v" and "first_frame" not in assets:
        raise ValueError("首帧模式需要上传首帧图片。")
    if mode == "l2v" and "last_frame" not in assets:
        raise ValueError("尾帧模式需要上传尾帧图片。")
    if mode == "fl2v" and not {"first_frame", "last_frame"} <= assets.keys():
        raise ValueError("首尾帧模式需要同时上传首帧和尾帧。")
    if mode == "reference" and not ({"reference_image", "reference_video"} & assets.keys()):
        raise ValueError("参考素材模式至少需要一张参考图或一个参考视频。")
    if mode == "reference" and {"reference_video", "reference_audio"} <= assets.keys():
        raise ValueError("参考视频使用其内置音频；暂不支持再叠加独立参考音频。")
    if (
        mode == "reference"
        and project.get("use_embedded_video_audio")
        and "reference_video" not in assets
    ):
        raise ValueError("只有上传参考视频后才能使用视频内置音频。")
    if mode == "hybrid" and not {
        "first_frame",
        "last_frame",
        "reference_image",
    } <= assets.keys():
        raise ValueError("Hybrid 模式需要首帧、尾帧和额外参考图。")
    if audio_policy == "lock_source" and not {"first_frame", "reference_audio"} <= assets.keys():
        raise ValueError("音频锁定需要首帧图片和源音频。")
    if strategy == "cloud" and (mode == "hybrid" or audio_policy == "lock_source"):
        raise ValueError("Hybrid 和精确音频锁定暂不支持云端加速策略。")


def quality_profile(project: dict) -> str:
    mode = project["mode"]
    assets = project["assets"]
    if project.get("audio_policy") == "lock_source":
        return COMMUNITY_TEMPLATES["audio_lock"]
    if mode == "t2v":
        return QUALITY_TEMPLATE
    if mode in {"i2v", "l2v", "fl2v"}:
        return COMMUNITY_TEMPLATES[mode]
    if mode == "hybrid":
        return COMMUNITY_TEMPLATES["hybrid"]
    if "reference_video" in assets:
        key = "reference_video_audio" if project.get("use_embedded_video_audio") else "reference_video"
        return COMMUNITY_TEMPLATES[key]
    key = "reference_image_audio" if "reference_audio" in assets else "reference_image"
    return COMMUNITY_TEMPLATES[key]


def build_workflow(
    project: dict,
    stage: str,
    workflow_root: Path,
) -> tuple[dict, str]:
    if stage not in {"preview", "proof", "local_768"}:
        raise ValueError(f"不支持的本地阶段：{stage}")
    turbo = stage == "preview" and uses_turbo_preview(project)
    if turbo:
        template_path = workflow_root / TURBO_TEMPLATE
    elif stage == "local_768" and project["mode"] == "t2v":
        template_path = workflow_root / QUALITY_768_TEMPLATE
    else:
        template_path = workflow_root / quality_profile(project)
    if not template_path.exists():
        raise FileNotFoundError(f"工作流模板不存在：{template_path}")

    workflow = json.loads(template_path.read_text(encoding="utf-8"))
    workflow = copy.deepcopy(workflow)
    width, height = (1344, 768) if stage == "local_768" else (864, 480)
    if project.get("orientation") == "portrait":
        width, height = height, width
    length = frames_for_duration(int(project["duration"]))
    prompt = project["prompt_approved"]
    seed = int(project["seed"])
    family_model = REF2VA_MODEL if project["mode"] in {"reference", "hybrid"} else FL2VA_MODEL

    for node in workflow.values():
        class_type = node.get("class_type")
        inputs = node.setdefault("inputs", {})
        if class_type == "UNETLoader":
            inputs["unet_name"] = family_model
        if class_type in {
            "MiniMaxH3AudioConditioningT8",
            "MiniMaxH3ImageToVideo",
            "MiniMaxH3ReferenceToVideo",
        }:
            inputs["prompt"] = prompt
            inputs["width"] = width
            inputs["height"] = height
            if not isinstance(inputs.get("length"), list):
                inputs["length"] = length
        if class_type == "RandomNoise":
            inputs["noise_seed"] = seed
        if class_type == "BasicScheduler":
            inputs["steps"] = 14
        if class_type == "MiniMaxH3AudioWindowT8":
            inputs["scene_duration_seconds"] = length / 24
        if class_type == "SaveVideo":
            inputs["filename_prefix"] = (
                f"video/h3-video-studio/{project['id']}/{stage}"
            )

    if turbo:
        _configure_turbo_assets(workflow, project)
    else:
        _configure_quality_assets(workflow, project)

    return workflow, template_path.name


def _configure_turbo_assets(workflow: dict, project: dict) -> None:
    conditioning_id, conditioning = _find_node(workflow, "MiniMaxH3AudioConditioningT8")
    inputs = conditioning["inputs"]
    mode = project["mode"]
    inputs["task_type"] = {
        "t2v": "T2VA",
        "i2v": "I2VA",
        "l2v": "L2VA",
        "fl2v": "FL2VA",
    }[mode]
    next_id = max(int(key) for key in workflow if str(key).isdigit()) + 1
    if mode in {"i2v", "fl2v"}:
        node_id = str(next_id)
        next_id += 1
        workflow[node_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": project["assets"]["first_frame"]["comfy_name"]},
        }
        inputs["first_frame"] = [node_id, 0]
    if mode in {"l2v", "fl2v"}:
        node_id = str(next_id)
        workflow[node_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": project["assets"]["last_frame"]["comfy_name"]},
        }
        inputs["last_frame"] = [node_id, 0]
    workflow[conditioning_id] = conditioning


def _configure_quality_assets(workflow: dict, project: dict) -> None:
    assets = project["assets"]
    mode = project["mode"]
    load_images = _nodes_of_type(workflow, "LoadImage")
    load_videos = _nodes_of_type(workflow, "LoadVideo")
    load_audios = _nodes_of_type(workflow, "LoadAudio")

    image_names: list[str] = []
    if mode == "i2v":
        image_names = [assets["first_frame"]["comfy_name"]]
    elif mode == "l2v":
        image_names = [assets["last_frame"]["comfy_name"]]
    elif mode == "fl2v":
        image_names = [
            assets["first_frame"]["comfy_name"],
            assets["last_frame"]["comfy_name"],
        ]
    elif mode == "hybrid":
        image_names = [
            assets["first_frame"]["comfy_name"],
            assets["last_frame"]["comfy_name"],
            assets["reference_image"]["comfy_name"],
        ]
    elif "reference_image" in assets:
        image_names = [assets["reference_image"]["comfy_name"]]

    for (_, node), filename in zip(load_images, image_names, strict=False):
        node["inputs"]["image"] = filename
    if load_videos and "reference_video" in assets:
        load_videos[0][1]["inputs"]["file"] = assets["reference_video"]["comfy_name"]
    if load_audios and "reference_audio" in assets:
        load_audios[0][1]["inputs"]["audio"] = assets["reference_audio"]["comfy_name"]


def _find_node(workflow: dict, class_type: str) -> tuple[str, dict]:
    for node_id, node in workflow.items():
        if node.get("class_type") == class_type:
            return node_id, node
    raise ValueError(f"工作流缺少节点：{class_type}")


def _nodes_of_type(workflow: dict, class_type: str) -> list[tuple[str, dict]]:
    return [
        (node_id, node)
        for node_id, node in sorted(
            workflow.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else 10**9,
        )
        if node.get("class_type") == class_type
    ]
