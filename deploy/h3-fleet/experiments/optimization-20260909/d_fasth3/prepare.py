from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import struct
import sys
from pathlib import Path


MODEL = "minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors"
REVISION = "f4cac997f880e93cf6940af61ee8d58ef31ff7f3"
MODEL_SHA256 = "7221ae65d78780354d51e5048d29728d9f1f8fb9baf50b1dd3df85f5101413d3"
MODEL_BYTES = 22898594920
VENDOR_SHA256 = "97c9d56fdc7c9a102e59bff9ac8d79503299514d061892088a03d99dcf415b0c"
BASE_TIMES = (0.999, 0.749, 0.500, 0.250, 0.0)
SOURCE_CACHE = Path("/home/ai/.local/share/h3-comparison-cache/20260909/source/d_fasth3")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sigmas(shift):
    return [shift * value / (1.0 + (shift - 1.0) * value) for value in BASE_TIMES]


def hardware_diagnostic(precision, capability):
    if precision == "nvfp4_dit":
        return {"status": "unsupported", "reason": "D4 excludes native NVFP4 DiT on Ampere/Ada; AWQ text encoder is a different path", "download_bytes": 0}
    require(precision == "int8_convrot", "unknown precision")
    require(tuple(capability) in {(8, 6), (8, 9)}, "D4 hardware scope is Ampere sm86 / Ada sm89")
    return {"status": "declared_not_runtime_validated", "backend": "comfy_kitchen.backends.cuda.sol_attn_chunked", "compute_capability": list(capability)}


def audit_gate_header(header):
    gates = {key: value for key, value in header.items() if "to_gate_compress" in key}
    expected = {f"blocks.{index}.attn.to_gate_compress.weight" for index in range(50)}
    require(expected <= gates.keys(), "missing one or more of 50 learned gate weights")
    for key, value in gates.items():
        parts = key.split(".")
        require(len(parts) >= 5 and parts[0] == "blocks" and parts[1].isdigit(), "unexpected gate namespace")
        require(0 <= int(parts[1]) < 50 and parts[2:4] == ["attn", "to_gate_compress"], "orphan gate tensor")
        require(isinstance(value, dict) and "shape" in value and "dtype" in value, "invalid gate descriptor")
        if key in expected:
            require(value["shape"] == [7168, 5376], f"incorrect learned gate shape: {key}")
    return {"status": "header_only", "gate_count": 50, "gate_tensor_keys": sorted(gates), "loaded_values_verified": False}


def read_header(path):
    with Path(path).open("rb") as stream:
        prefix = stream.read(8)
        require(len(prefix) == 8, "truncated safetensors prefix")
        size = struct.unpack("<Q", prefix)[0]
        require(0 < size <= 16 * 1024 * 1024, "invalid or excessive safetensors header")
        payload = stream.read(size)
        require(len(payload) == size, "truncated safetensors header")
    return json.loads(payload)


def audit_core(model_source, detection_source):
    model_tree = ast.parse(model_source)
    detection_tree = ast.parse(detection_source)
    classes = {node.name: node for node in model_tree.body if isinstance(node, ast.ClassDef)}
    failures = []
    for class_name in ("Attention", "DiTBlock", "MiniMaxH3Model"):
        constructor = next((node for node in classes[class_name].body if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
        if constructor is None or "gate_compress" not in [arg.arg for arg in constructor.args.args]:
            failures.append(f"{class_name} does not accept gate_compress")
    if "self.to_gate_compress = operations.Linear" not in model_source:
        failures.append("gate layer construction missing")
    for class_name in ("DiTBlock", "MiniMaxH3Model"):
        if not any(isinstance(node, ast.keyword) and node.arg == "gate_compress" and isinstance(node.value, ast.Name) and node.value.id == "gate_compress" for node in ast.walk(classes[class_name])):
            failures.append(f"{class_name} does not propagate gate_compress")
    if not any(isinstance(node, ast.Constant) and node.value == "{}blocks.0.attn.to_gate_compress.weight" for node in ast.walk(detection_tree)):
        failures.append("gate checkpoint detection missing")
    return {"status": "blocked" if failures else "static_structure_pass", "failures": failures, "loaded_values_verified": False}


def _one(graph, class_type):
    found = [(node_id, node) for node_id, node in graph.items() if node.get("class_type") == class_type]
    require(len(found) == 1, f"expected exactly one {class_type}")
    return found[0]


def build_workflow(template, prompt, *, seed, filename_prefix="video/h3-comparison/D4"):
    require(isinstance(prompt, str) and prompt.strip(), "frozen prompt text is required")
    require(isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < 2**64, "invalid explicit seed")
    require(filename_prefix.startswith("video/h3-comparison/") and ".." not in filename_prefix and "\\" not in filename_prefix, "output prefix must stay under video/h3-comparison/")
    graph = copy.deepcopy(template)
    allowed = {"VAELoader", "CLIPLoader", "UNETLoader", "LoraLoaderBypassModelOnly", "MiniMaxH3AudioConditioningT8", "MiniMaxH3DualClockSamplerT8", "RandomNoise", "BasicGuider", "SamplerCustomAdvanced", "MiniMaxH3AVDecodeT8", "CreateVideo", "SaveVideo"}
    require(all(node.get("class_type") in allowed for node in graph.values()), "unexpected template node; refusing inherited attention/cache/reference patches")
    condition_id, conditioning = _one(graph, "MiniMaxH3AudioConditioningT8")
    condition = conditioning["inputs"]
    condition_fields = {"prompt", "width", "height", "length", "task_type", "audio_mode", "audio_denoise_strength", "add_source_as_reference", "prompt_primary_audio_ordinal", "strict_prompt_tags", "ref_image_size", "reference_video_policy", "clip", "video_vae", "audio_vae"}
    require(condition.keys() <= condition_fields, "unknown conditioning inputs; references are not accepted")
    require(condition.get("task_type") == "T2VA" and condition.get("audio_mode") == "native", "D4 supports plain T2VA with native audio only")
    require(condition.get("audio_denoise_strength", 1.0) == 1.0 and not condition.get("add_source_as_reference", False), "D4 requires generated audio without source conditioning")
    require(condition.get("length") == 362, "D4 requires the common 362-frame full15 template, never 124/345 frames")
    require(not any(key in condition for key in ("image", "first_frame", "last_frame", "reference", "reference_images", "source_audio", "source_video")), "reference conditioning is unsupported")
    _, video = _one(graph, "CreateVideo")
    require(video["inputs"].get("fps") == 24, "common full15 contract requires 24 fps")
    require(graph[condition["video_vae"][0]]["inputs"].get("vae_name") == "minimax_h3_video_vae_fp16.safetensors", "main D4 comparison must retain FP16 video VAE")
    loader_id, loader = _one(graph, "UNETLoader")
    lora_id, lora = _one(graph, "LoraLoaderBypassModelOnly")
    require(lora["inputs"].get("model") == [loader_id, 0], "unexpected LoRA input")
    sampler_id, sampler = _one(graph, "MiniMaxH3DualClockSamplerT8")
    require(sampler["inputs"].get("model") == [lora_id, 0], "unexpected sampler model")
    require(sampler["inputs"].get("av_latent") == [condition_id, 1], "unexpected sampler latent")
    sample_id, sample = _one(graph, "SamplerCustomAdvanced")
    require(sample["inputs"].get("sigmas") == [sampler_id, 2], "unexpected sigma wiring")
    guider_id, guider = _one(graph, "BasicGuider")
    require(guider["inputs"].get("model") == [sampler_id, 0] and sample["inputs"].get("guider") == [guider_id, 0], "unexpected guider wiring")
    require(all(value != [lora_id, 0] or node_id == sampler_id for node_id, node in graph.items() for value in node.get("inputs", {}).values()), "extra LoRA consumer")
    condition["prompt"] = prompt
    loader["inputs"].update(unet_name=MODEL, weight_dtype="default")
    graph[lora_id] = {"class_type": "D4StrictVSA", "inputs": {"model": [loader_id, 0], "checkpoint": MODEL}}
    sampler["inputs"].update(steps=4, shift_video=12.0, shift_audio=3.0, sampler_name="dual_clock_euler", scheduler="native_flow")
    sigma_id = "d4_exact_sigmas"
    require(sigma_id not in graph, "D4 node id collision")
    graph[sigma_id] = {"class_type": "D4FastH3Sigmas", "inputs": {}}
    sample["inputs"]["sigmas"] = [sigma_id, 0]
    contract_id = "d4_sampling_contract"
    require(contract_id not in graph, "D4 contract node id collision")
    graph[contract_id] = {"class_type": "D4SamplingContract", "inputs": {"model": [sampler_id, 0], "sigmas": [sigma_id, 0]}}
    guider["inputs"]["model"] = [contract_id, 0]
    _, noise = _one(graph, "RandomNoise")
    noise["inputs"]["noise_seed"] = seed
    _, save = _one(graph, "SaveVideo")
    save["inputs"]["filename_prefix"] = filename_prefix
    for node in graph.values():
        node.pop("_meta", None)
    receipt = {
        "candidate": "D4", "status": "prepared_not_submitted", "ready_for_inference": False,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "template_sha256": hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest(),
        "checkpoint": {"repo": "Kijai/MiniMax-H3-experimental", "revision": REVISION, "filename": MODEL, "bytes": MODEL_BYTES, "sha256": MODEL_SHA256},
        "contract": {"frames": 362, "fps": 24, "nominal_seconds": 15, "container_seconds": 362 / 24, "base_times": list(BASE_TIMES), "video_sigmas": sigmas(12), "audio_sigmas": sigmas(3), "transformer_forwards": 4, "cfg": 1, "sparsity": 0.9, "tile_rows": 64, "tail": False, "t8_ema": False, "initial_noise": "unscaled_fp32_for_both_streams", "audio_velocity_is_raw": True, "audio_carry_scale": 1.0, "stochastic_renoise": False, "bitwise_cross_backend_parity": False},
        "pending": ["isolated patched core and native Kitchen kernel", "complete checkpoint hash and all gate load audit", "CPU/meta loader verification", "master hardware/offload admission", "runtime per-block VSA counters and no fallback", "full15 audiovisual acceptance; model precision and RNG correspondence remain unverified"],
    }
    return graph, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="D4 offline preparation only; no network, GPU, service or queue operations")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--template", type=Path, required=True)
    build.add_argument("--prompt-file", type=Path, required=True)
    build.add_argument("--seed", type=int, required=True)
    build.add_argument("--filename-prefix", default="video/h3-comparison/D4")
    core = commands.add_parser("audit-core")
    core.add_argument("--model-source", type=Path, required=True)
    core.add_argument("--detection-source", type=Path, required=True)
    header = commands.add_parser("audit-header")
    header.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            graph, receipt = build_workflow(json.loads(args.template.read_text()), args.prompt_file.read_bytes().decode("utf-8"), seed=args.seed, filename_prefix=args.filename_prefix)
            result = {"workflow": graph, "receipt": receipt}
        elif args.command == "audit-core":
            result = audit_core(args.model_source.read_text(), args.detection_source.read_text())
        else:
            result = audit_gate_header(read_header(args.checkpoint))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "blocked" else 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
