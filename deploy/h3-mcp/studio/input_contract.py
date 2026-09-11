from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException


MODES = ("t2v", "i2v", "l2v", "fl2v", "reference", "hybrid")
REQUIRED = {"t2v": set(), "i2v": {"first_frame"}, "l2v": {"last_frame"},
            "fl2v": {"first_frame", "last_frame"}, "hybrid": {"first_frame", "last_frame", "reference_image"}}
ALLOWED = {**REQUIRED, "reference": {"reference_image", "reference_video", "reference_audio"}}
PROFILE_IDS = {mode: "H3_" + mode.upper() + "_QUALITY14" for mode in MODES if mode != "t2v"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate(project):
    mode, assets = project.get("mode"), project.get("assets", {})
    if mode not in MODES or not isinstance(assets, dict):
        raise HTTPException(400, "unsupported input mode")
    allowed = set(ALLOWED[mode])
    if mode in {"i2v", "fl2v"} and project.get("audio_policy") == "lock_source":
        allowed.add("reference_audio")
    if set(assets) - allowed or not REQUIRED.get(mode, set()) <= assets.keys():
        raise HTTPException(400, "assets do not match selected mode")
    if mode == "reference":
        sources = {"reference_image", "reference_video"} & assets.keys()
        if len(sources) != 1 or ("reference_video" in assets and "reference_audio" in assets):
            raise HTTPException(400, "select exactly one reference image or video; do not silently discard sources")
    if project.get("audio_policy") == "lock_source" and not {"first_frame", "reference_audio"} <= assets.keys():
        raise HTTPException(400, "locked source audio requires first frame and audio")
    if project.get("use_embedded_video_audio") and (mode != "reference" or "reference_video" not in assets):
        raise HTTPException(400, "使用视频原声必须绑定参考视频；不会忽略音轨设置")


def validate_media(project):
    validate(project)
    role = "reference_video" if project.get("use_embedded_video_audio") else "reference_audio" if project.get("audio_policy") == "lock_source" else None
    if role:
        metadata = project["assets"][role].get("metadata") or {}
        if metadata.get("has_audio") is not True:
            raise HTTPException(400, "所选素材没有经过解码验证的音轨；请更换素材或明确修改音频设置，未提交生成")


def validate_execution_media(project):
    validate_media(project)
    video = project.get("assets", {}).get("reference_video")
    if video is None:
        return
    metadata = video.get("metadata") or {}
    if metadata.get("is_cfr_24") is not True:
        raise HTTPException(409, "当前完整模型要求24fps参考帧；原视频不是已验证的恒定24fps，未转换、变速或提交生成。请提供明确确认的24fps素材")
    if project.get("use_embedded_video_audio") and metadata.get("audio_track_count") != 1:
        raise HTTPException(409, "使用视频原声需唯一音轨，不能擅自选择或丢弃多个音轨，未提交生成")


def snapshot(project):
    return {**({"preview_recipe_id": project["preview_recipe_id"]} if project.get("preview_recipe_id") else {}),
            **{key: project.get(key) for key in ("mode", "duration", "orientation", "audio_policy", "use_embedded_video_audio",
            "prompt_original", "prompt_ir", "seed", "recipe_id", "connector_recipe_version", "execution_profile")},
            "assets": {kind: {key: asset.get(key) for key in ("asset_id", "sha256", "size", "comfy_name")}
                       for kind, asset in sorted(project.get("assets", {}).items())}}


def public_assets(project):
    return [{"kind": kind, **{key: asset.get(key) for key in ("asset_id", "sha256", "name", "size", "mime", "metadata")},
             "preview_url": "/api/projects/" + project["id"] + "/input-assets/" + kind}
            for kind, asset in project.get("assets", {}).items()]


def profile(module, project):
    validate(project)
    if project.get("preview_recipe_id"):
        accelerated = __import__(module.__package__ + ".accelerated_i2v", fromlist=["profile"])
        if (project["preview_recipe_id"] not in accelerated.RECIPES or project["mode"] != "i2v" or project["audio_policy"] != "native"
                or project["duration"] != 15 or project["orientation"] != "portrait" or project.get("render_plan")):
            raise HTTPException(409, "配方首帧测试仅接受15秒480×864原生音轨，不更改其他规格")
        template = Path(module.__file__).parent / accelerated.RECIPES[project["preview_recipe_id"]][1]
        if not template.is_file():
            raise HTTPException(409, "首帧加速模板尚未发布；未提交生成")
        return accelerated.profile(template.read_bytes(), project["preview_recipe_id"])
    workflow_module = __import__(module.__package__ + ".workflows", fromlist=["quality_profile"])
    template = workflow_module.quality_profile(project)
    path = module.SETTINGS.workflow_root / template
    if not path.is_file():
        raise HTTPException(409, "multimodal workflow template unavailable")
    variant = ""
    if project.get("audio_policy") == "lock_source":
        variant = "_AUDIO_LOCK"
    elif project["mode"] == "reference":
        variant = "_VIDEO" if "reference_video" in project["assets"] else "_IMAGE"
        if project.get("use_embedded_video_audio") or "reference_audio" in project["assets"]:
            variant += "_AUDIO"
    result = {"profile_id": "H3_" + project["mode"].upper() + variant + "_QUALITY14", "version": hashlib.sha256(path.read_bytes()).hexdigest(),
            "template": template, "steps": 14, "family": "REF2VA" if project["mode"] in {"reference", "hybrid"} else "FL2VA",
            "mode": project["mode"], "audio_policy": project["audio_policy"]}
    return result


def check_graph_assets(graph, project):
    expected = {asset["comfy_name"] for asset in project.get("assets", {}).values()}
    used = []
    loaders = {"LoadImage": "image", "LoadVideo": "file", "LoadAudio": "audio"}
    for node in graph.values():
        if node.get("class_type") in loaders:
            name = node.get("inputs", {}).get(loaders[node["class_type"]])
            if not isinstance(name, str):
                raise HTTPException(409, "asset loader must bind an immutable uploaded filename")
            used.append(name)
    if len(used) != len(expected) or set(used) != expected:
        raise HTTPException(409, "workflow does not consume exactly the bound assets")
    ancestors = set()

    def visit(identifier):
        if identifier in ancestors:
            return
        ancestors.add(identifier)
        for value in graph[identifier].get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in graph:
                visit(value[0])

    for identifier, node in graph.items():
        if node.get("class_type") == "SaveVideo":
            visit(identifier)
    if any(identifier not in ancestors for identifier, node in graph.items() if node.get("class_type") in loaders):
        raise HTTPException(409, "uploaded asset is disconnected from video output")


def bind_graph_assets(graph, project):
    validate(project)
    conditioners = [entry for entry in graph.values() if entry.get("class_type") in {
        "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AudioConditioningT8"}]
    if len(conditioners) != 1:
        raise HTTPException(409, "ambiguous multimodal conditioning node")
    inputs = conditioners[0]["inputs"]
    if project["mode"] == "fl2v" and project.get("audio_policy") == "lock_source" and "last_frame" not in inputs:
        identifier = str(max(int(key) for key in graph if key.isdigit()) + 1)
        graph[identifier] = {"class_type": "LoadImage", "inputs": {"image": project["assets"]["last_frame"]["comfy_name"]}}
        inputs.update(last_frame=[identifier, 0], task_type="FL2VA")
    roles = {"first_frame": "first_frame", "last_frame": "last_frame", "reference_image": "ref_images.ref_image_0",
             "reference_video": "ref_videos.ref_video_0", "reference_audio": "drive_audio" if project.get("audio_policy") == "lock_source" else "ref_audios.ref_audio_0"}
    loaders = {"first_frame": ("LoadImage", "image"), "last_frame": ("LoadImage", "image"),
               "reference_image": ("LoadImage", "image"), "reference_video": ("LoadVideo", "file"), "reference_audio": ("LoadAudio", "audio")}

    def sources(link, expected, visited=None):
        visited = set() if visited is None else visited
        if not isinstance(link, list) or len(link) != 2 or link[0] not in graph or link[0] in visited:
            return set()
        identifier = link[0]
        visited.add(identifier)
        node = graph[identifier]
        if node.get("class_type") == expected:
            return {identifier}
        result = set()
        for value in node.get("inputs", {}).values():
            result.update(sources(value, expected, visited))
        return result

    assigned = set()
    for role, asset in project["assets"].items():
        node_type, field = loaders[role]
        matches = sources(inputs.get(roles[role]), node_type)
        if len(matches) != 1 or assigned.intersection(matches):
            raise HTTPException(409, "asset role is disconnected or ambiguous: " + role)
        identifier = next(iter(matches))
        graph[identifier]["inputs"][field] = asset["comfy_name"]
        assigned.add(identifier)
    check_graph_assets(graph, project)


def check_quality_controls(graph, project):
    schedulers = [entry["inputs"] for entry in graph.values() if entry.get("class_type") == "BasicScheduler"]
    weights = [entry["inputs"].get("unet_name") for entry in graph.values() if entry.get("class_type") == "UNETLoader"]
    expected = "minimax_h3_" + ("ref2va" if project["mode"] in {"reference", "hybrid"} else "fl2va") + "_int8_convrot.safetensors"
    if len(schedulers) != 1 or schedulers[0].get("steps") != 14 or weights != [expected]:
        raise HTTPException(409, "multimodal workflow must use its dedicated full model and 14 steps")
    if any("LoraLoader" in entry.get("class_type", "") or entry.get("class_type") == "MiniMaxH3DualClockSamplerT8" for entry in graph.values()):
        raise HTTPException(409, "multimodal quality workflow cannot use an acceleration recipe")


def quality_graph(module, project):
    current = profile(module, project)
    if project.get("execution_profile") != current:
        raise HTTPException(409, "execution profile changed; save and approve inputs again")
    if project.get("preview_recipe_id"):
        accelerated = __import__(module.__package__ + ".accelerated_i2v", fromlist=["execution_prompt"])
        graph = json.loads((Path(module.__file__).parent / current["template"]).read_bytes())
        graph["6"]["inputs"]["prompt"] = accelerated.execution_prompt(project["prompt_approved"], project["preview_recipe_id"])
        graph["8"]["inputs"]["noise_seed"] = project["seed"]
        graph["13"]["inputs"]["filename_prefix"] = "video/h3-video-studio/" + project["id"] + "/preview"
        bind_graph_assets(graph, project)
        return graph, current["template"]
    graph, template = module.build_workflow(project, "proof", module.SETTINGS.workflow_root)
    bind_graph_assets(graph, project)
    check_quality_controls(graph, project)
    return graph, template
