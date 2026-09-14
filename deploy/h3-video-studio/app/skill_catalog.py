from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / "prompt_profiles.json").read_text())
SOURCES = json.loads((ROOT / "skill_sources.json").read_text())
RULESET_HASH = hashlib.sha256((ROOT / "prompt_profiles.json").read_bytes()).hexdigest()
BASE_SKILLS = ["h3-prompt-writing", "cinematic-shot-prompt-expert"]
GROUPS = [
    ("h3-prompt-writing", "H3 提示词编写", "h3", [], "将需求组织为画面、环境声和音乐，保持时间线与参考素材约束。"),
    ("cinematic-shot-prompt-expert", "电影级镜头", "cinematography", [], "规划可执行的景别、运镜、构图、光线与连续动作。"),
    ("minimax-h3", "H3 视频策划", "h3", ["video-generation"], "通用视频的声画结构策划；不会直接调用生成脚本。"),
    ("image-to-video", "首帧视频策划", "mode:i2va", ["minimax-h3-image-to-video"], "从用户提供的首帧展开可观察的动作。"),
    ("first-last-frame-to-video", "首尾帧衔接", "mode:fl2va", [], "描述首帧到尾帧的连续过程，不仅描述两个静止状态。"),
    ("image-reference-to-video", "参考素材策划", "mode:ref2va", ["minimax-h3-image-reference-to-video"], "明确人物、产品、风格等参考用途，不假装已经分析素材。"),
    ("seeding-video", "种草视频", "creative:seeding", ["minimax-h3-seeding-video"], "可信的发现过程、使用细节和推荐，不编造功效。"),
    ("social-commerce-video", "带货视频", "creative:social_commerce", ["minimax-h3-social-commerce-video"], "痛点、使用过程、可观察收益和行动引导。"),
    ("ecommerce-video", "电商产品视频", "creative:ecommerce", [], "产品结构、材质、演示动作、场景与已知卖点。"),
    ("tvc-video", "TVC 广告片", "creative:tvc", [], "品牌叙事与有目的的镜头，保留稳定的产品收尾。"),
    ("ai-ad-video", "创意广告", "creative:ai_ad", [], "快速建立主题、一个鲜明视觉变化与清晰品牌收尾。"),
    ("short-drama-video", "短剧脚本", "creative:short_drama", [], "人物关系、戏剧动作、对白与服装场景连续性。"),
    ("dynamic-comic-video", "动态漫画", "creative:dynamic_comic", [], "锁定角色、画风和分镜，再描述有限而清楚的动作。"),
    ("minimax-h3-video-generation-and-editing", "H3 生成与编辑工具", None, [], "仅归档；第三方执行脚本未启用。"),
    ("image-prompt-skill__skillhub", "图片提示词工具", None, [], "仅归档；尚无对应的受控图片策划规则。"),
]


def catalog():
    origins = {item["directory"]: item for item in SOURCES["skills"]}
    return [{"id": identifier, "name": name, "description": description, "available": profile is not None,
             "baseline": identifier in BASE_SKILLS,
             "execution_kind": "distilled_prompt_guidance" if profile else "archive_only",
             "ruleset_sha256": RULESET_HASH,
             "sources": [origins[key] for key in [identifier, *aliases] if key in origins]}
            for identifier, name, profile, aliases, description in GROUPS]


def selected_skills(identifiers):
    available = {item["id"]: item for item in catalog() if item["available"]}
    if not isinstance(identifiers, list) or len(identifiers) > 6 or any(not isinstance(item, str) or item not in available for item in identifiers):
        raise ValueError("仅可选择已启用的 Skills 安全规则，最多六项。")
    selected = list(dict.fromkeys([*BASE_SKILLS, *identifiers]))
    if len(selected) > 8:
        raise ValueError("组合 Skills 数量超出策划预算。")
    return [available[identifier] for identifier in selected]


def rules_for(skills):
    categories = PROFILES["categories"]
    profiles = {row[0]: row[2] for row in GROUPS}
    result = {}
    for skill in skills:
        profile = profiles[skill["id"]]
        if profile.startswith("mode:"):
            result[skill["id"]] = categories["h3"]["supported_modes"][profile.split(":")[1]]
        elif profile.startswith("creative:"):
            result[skill["id"]] = {"common_rules": categories["commercial"]["global_rules"],
                                  "profile_rules": categories["commercial"]["profiles"][profile.split(":")[1]]}
        else:
            result[skill["id"]] = categories[profile]
    return result
