"""D4 精确图校验；仅返回 runner shape，不导入运行时或提交生成。"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


SAVE_NODE = "13"
SAMPLER_NODE = "10"
SAMPLER_SETUP_NODE = "7"
MUTABLE_INPUTS = (("6", "prompt"), ("8", "noise_seed"), (SAVE_NODE, "filename_prefix"))
MODEL = "minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors"
APPROVED_GRAPH = {
    "1": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
    "2": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
    "3": {"class_type": "CLIPLoader", "inputs": {
        "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
    "4": {"class_type": "UNETLoader", "inputs": {"unet_name": MODEL, "weight_dtype": "default"}},
    "5": {"class_type": "D4StrictVSA", "inputs": {"model": ["4", 0], "checkpoint": MODEL}},
    "6": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
        "prompt": "FROZEN CONTRACT PLACEHOLDER", "width": 480, "height": 864, "length": 362,
        "task_type": "T2VA", "audio_mode": "native", "audio_denoise_strength": 1.0,
        "add_source_as_reference": False, "prompt_primary_audio_ordinal": 0, "strict_prompt_tags": True,
        "ref_image_size": "match", "reference_video_policy": "official_2_to_15s",
        "clip": ["3", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]}},
    "7": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {
        "steps": 4, "shift_video": 12.0, "shift_audio": 3.0, "model": ["5", 0],
        "av_latent": ["6", 1], "sampler_name": "dual_clock_euler", "scheduler": "native_flow"}},
    "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
    "9": {"class_type": "BasicGuider", "inputs": {
        "model": ["d4_sampling_contract", 0], "conditioning": ["6", 0]}},
    "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["8", 0], "guider": ["9", 0], "sampler": ["7", 1],
        "sigmas": ["d4_exact_sigmas", 0], "latent_image": ["6", 1]}},
    "11": {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {
        "av_latent": ["10", 0], "video_vae": ["1", 0], "audio_vae": ["2", 0]}},
    "12": {"class_type": "CreateVideo", "inputs": {
        "images": ["11", 0], "audio": ["11", 1], "fps": 24.0, "bit_depth": 8}},
    "13": {"class_type": "SaveVideo", "inputs": {
        "video": ["12", 0], "filename_prefix": "video/h3-comparison/D4", "format": "auto", "codec": "auto"}},
    "d4_exact_sigmas": {"class_type": "D4FastH3Sigmas", "inputs": {}},
    "d4_sampling_contract": {"class_type": "D4SamplingContract", "inputs": {
        "model": ["7", 0], "sigmas": ["d4_exact_sigmas", 0]}},
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def relative_name(value):
    return (
        isinstance(value, str) and value.startswith("video/h3-comparison/")
        and ".." not in value and "\\" not in value and ":" not in value
        and not any(ord(character) < 32 for character in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def exact_contract(candidate, approved):
    if isinstance(approved, dict):
        return (type(candidate) is dict and candidate.keys() == approved.keys()
                and all(exact_contract(candidate[key], value) for key, value in approved.items()))
    if isinstance(approved, list):
        return (type(candidate) is list and len(candidate) == len(approved)
                and all(exact_contract(actual, expected) for actual, expected in zip(candidate, approved)))
    if type(approved) is float:
        return type(candidate) in (int, float) and candidate == approved
    return type(candidate) is type(approved) and candidate == approved


def validate_workflow(graph, case=None):
    if case not in (None, "D4"):
        raise ValueError("D4 validator cannot authorize another recipe")
    if type(graph) is not dict or graph.keys() != APPROVED_GRAPH.keys():
        raise ValueError("D4 requires the exact frozen node IDs and node count")
    candidate = copy.deepcopy(graph)
    try:
        for node in candidate.values():
            if type(node) is not dict or type(node.get("inputs")) is not dict:
                raise ValueError("invalid API node")
        prompt = candidate["6"]["inputs"]["prompt"]
        seed = candidate["8"]["inputs"]["noise_seed"]
        prefix = candidate[SAVE_NODE]["inputs"]["filename_prefix"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("nonempty common prompt required; never rewritten")
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("seed must be uint64")
        if not relative_name(prefix):
            raise ValueError("unsafe output prefix; require video/h3-comparison/ namespace")
        for identifier, name in MUTABLE_INPUTS:
            candidate[identifier]["inputs"][name] = APPROVED_GRAPH[identifier]["inputs"][name]
        if not exact_contract(candidate, APPROVED_GRAPH):
            raise ValueError("unaudited D4 node, wiring, field or parameter change")
    except (KeyError, TypeError) as error:
        raise ValueError("invalid D4 API workflow") from error
    return {"width": 480, "height": 864, "length": 362, "fps": 24,
            "steps": 4, "shift_video": 12, "shift_audio": 3, "save_node": SAVE_NODE}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--case", default="D4")
    args = parser.parse_args(argv)
    try:
        graph = json.loads(args.workflow.read_bytes(), object_pairs_hook=unique_object)
        shape = validate_workflow(graph, args.case)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(shape, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
