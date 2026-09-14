from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re


CONDITIONERS = {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AudioConditioningT8"}
DYNAMIC = {**{name: {"prompt", "width", "height", "length"} for name in CONDITIONERS},
           "RandomNoise": {"noise_seed"}, "SaveVideo": {"filename_prefix"}, "LoadImage": {"image"},
           "LoadVideo": {"file"}, "LoadAudio": {"audio"}, "MiniMaxH3AudioWindowT8": {"scene_duration_seconds"}}
MODES = {"i2v", "l2v", "fl2v", "reference", "hybrid"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def structure(graph):
    if not isinstance(graph, dict) or not graph:
        raise ValueError("multimodal API graph required")
    result = copy.deepcopy(graph)
    for node in result.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError("invalid multimodal graph node")
        for field in DYNAMIC.get(node.get("class_type"), set()):
            if field in node["inputs"] and not isinstance(node["inputs"][field], list):
                node["inputs"][field] = "__H3_INPUT__"
    return digest(result)


class MultimodalCatalog:
    def __init__(self, base, path=None):
        self.base = base
        self.entries = {}
        self.unavailable = []
        if path is None:
            return
        path = Path(path)
        document = json.loads(path.read_bytes())
        if document.get("schema_version") != 1:
            raise ValueError("unsupported multimodal catalog version")
        self.unavailable = document.get("unavailable_profiles", [])
        for entry in document["profiles"]:
            identifier = entry["profile_id"]
            accelerated = re.fullmatch(r"H3_I2V_(?:A4(?:_C[01])?_TURBO4|B8_VDN8)", identifier) is not None
            if accelerated:
                from .accelerated_i2v import PROFILE_RECIPES, RECIPES
            if (not accelerated and not re.fullmatch(r"H3_(?:I2V|L2V|FL2V|REFERENCE|HYBRID)(?:_[A-Z]+)*_QUALITY14", identifier)) or identifier in self.entries:
                raise ValueError("invalid or duplicate multimodal profile")
            if entry["mode"] not in MODES or entry["sampling"]["steps"] != (RECIPES[PROFILE_RECIPES[identifier]][2] if accelerated else 14) or not entry["weights"]:
                raise ValueError("multimodal profiles require a full model and 14 steps")
            template = (path.parent / entry["template"]).resolve()
            if not template.is_relative_to(path.parent.resolve()) or template.is_symlink():
                raise ValueError("multimodal template outside release")
            raw = template.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["template_sha256"] or structure(json.loads(raw)) != entry["structure_sha256"]:
                raise ValueError("multimodal template pin mismatch")
            graph = json.loads(raw)
            if accelerated:
                from .accelerated_i2v import validate
                source = validate(graph, self.base, PROFILE_RECIPES[identifier])
                if (entry["mode"] != "i2v" or entry.get("base_recipe_digest") != source["recipe_digest"]
                        or entry.get("asset_roles") != {"first_frame": "20"}
                        or entry.get("audio_policy") != "native"):
                    raise ValueError("accelerated first-frame source binding mismatch")
                self.entries[identifier] = {**entry, "recipe_id": identifier, "recipe_digest": digest(entry)}
                self.validate(identifier, graph, entry["version"])
                continue
            schedules = [node["inputs"].get("steps") for node in graph.values() if node.get("class_type") == "BasicScheduler"]
            expected_weight = "minimax_h3_" + ("ref2va" if entry["mode"] in {"reference", "hybrid"} else "fl2va") + "_int8_convrot.safetensors"
            models = [node["inputs"].get("unet_name") for node in graph.values() if node.get("class_type") == "UNETLoader"]
            if schedules != [14] or models != [expected_weight] or expected_weight not in {weight["filename"] for weight in entry["weights"]}:
                raise ValueError("profile must bind the dedicated full model and 14-step scheduler")
            if any("LoraLoader" in node.get("class_type", "") or node.get("class_type") == "MiniMaxH3DualClockSamplerT8" for node in graph.values()):
                raise ValueError("multimodal full profiles cannot use acceleration recipes")
            self.entries[identifier] = {**entry, "recipe_id": identifier, "recipe_digest": digest(entry)}
            self.validate(identifier, json.loads(raw), entry["version"])

    def get(self, identifier):
        if identifier in self.entries:
            return copy.deepcopy(self.entries[identifier])
        return self.base.get(identifier)

    def public(self):
        result = self.base.public()
        result["multimodal_profiles"] = [{key: entry.get(key) for key in (
            "profile_id", "version", "mode", "family", "audio_policy", "sampling", "recipe_digest")}
            for entry in self.entries.values()]
        result["unavailable_multimodal_profiles"] = copy.deepcopy(self.unavailable)
        return result

    def build(self, *args, **kwargs):
        return self.base.build(*args, **kwargs)

    def validate(self, identifier, graph, version=None):
        if identifier not in self.entries:
            return self.base.validate(identifier, graph, version)
        entry = self.entries[identifier]
        if entry.get("preview_recipe_id"):
            from .accelerated_i2v import PROFILE_RECIPES, validate
            validate(graph, self.base, PROFILE_RECIPES[identifier])
        if version != entry["version"] or structure(graph) != entry["structure_sha256"]:
            raise ValueError("multimodal profile or graph version mismatch")
        conditioners = [node["inputs"] for node in graph.values() if node["class_type"] in CONDITIONERS]
        if len(conditioners) != 1:
            raise ValueError("ambiguous multimodal conditioner")
        condition = conditioners[0]
        if (condition["width"], condition["height"]) not in {(480, 864), (864, 480)}:
            raise ValueError("unsupported multimodal preview dimensions")
        prompt = condition["prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000 or "\0" in prompt:
            raise ValueError("invalid multimodal prompt")
        frames = condition["length"]
        windows = [node["inputs"]["scene_duration_seconds"] for node in graph.values() if node["class_type"] == "MiniMaxH3AudioWindowT8"]
        if isinstance(frames, list):
            if len(windows) != 1 or type(windows[0]) not in (int, float):
                raise ValueError("audio window does not define a video duration")
            frames = round(windows[0] * 24)
            if abs(frames / 24 - windows[0]) > 0.000001:
                raise ValueError("audio window is not aligned to the approved frame count")
        if type(frames) is not int or frames not in range(107, 363, 17):
            raise ValueError("unsupported multimodal frame count")
        assets = []
        roles = {}
        for node in graph.values():
            inputs = node["inputs"]
            if node["class_type"] == "RandomNoise" and (type(inputs["noise_seed"]) is not int or not 0 <= inputs["noise_seed"] < 2**64):
                raise ValueError("invalid multimodal seed")
            if node["class_type"] == "SaveVideo":
                prefix = inputs["filename_prefix"]
                if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
                    raise ValueError("invalid multimodal output prefix")
            if node["class_type"] in {"LoadImage", "LoadVideo", "LoadAudio"}:
                field = next(iter(DYNAMIC[node["class_type"]]))
                filename = inputs[field]
                if not isinstance(filename, str) or not re.fullmatch(r"asset_[a-f0-9]{32}\.(?:png|jpe?g|webp|mp4|mov|mkv|webm|wav|mp3|m4a|aac|flac|ogg)", filename):
                    raise ValueError("immutable uploaded asset filename required")
                assets.append(filename)
        for role, node_id in entry["asset_roles"].items():
            node = graph[node_id]
            if node["class_type"] not in {"LoadImage", "LoadAudio", "LoadVideo"}:
                raise ValueError("asset role must point to a pinned loader")
            roles[role] = node["inputs"][next(iter(DYNAMIC[node["class_type"]]))]
        if not assets or len(assets) != len(set(assets)):
            raise ValueError("missing or duplicated multimodal asset binding")
        if sorted(roles.values()) != sorted(assets):
            raise ValueError("every input loader must have an explicit role")
        return {"recipe_id": identifier, "recipe_version": entry["version"], "recipe_digest": entry["recipe_digest"],
                "profile_id": identifier, "profile_version": entry["version"], "mode": entry["mode"],
                "graph_sha256": digest(graph), "structure_sha256": entry["structure_sha256"],
                "template_sha256": entry["template_sha256"], "input_filenames": assets, "input_roles": roles,
                "width": condition["width"], "height": condition["height"], "fps": 24, "frame_count": frames,
                "actual_duration": frames / 24, "steps": entry["sampling"]["steps"], "hardware_qualification": "not_evaluated"}

    def freeze(self, identifier, graph):
        return copy.deepcopy(graph), self.validate(identifier, graph, self.get(identifier)["version"])
