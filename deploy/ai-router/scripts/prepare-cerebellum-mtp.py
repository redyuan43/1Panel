#!/usr/bin/env python3
"""Append a verified Qwen35MoE MTP head without requantizing the main model."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys


HEAD_REPOSITORY = "havenoammo/Qwen3.6-35B-A3B-MTP-GGUF"
HEAD_REVISION = "a529a1734ce45a423a27a399d462791201d6995b"
HEAD_FILENAME = "35BA3B-MTP.gguf"
HEAD_SHA256 = "fb16c34255b1a3bc52e64bd1a9d6f288c67670c0b3c9d8d067fff2c4deeca435"
CHUNK_SIZE = 8 * 1024 * 1024
ARCH = "qwen35moe"
BLOCK_COUNT = ARCH + ".block_count"
NEXTN = ARCH + ".nextn_predict_layers"


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while data := handle.read(CHUNK_SIZE):
            digest.update(data)
    return digest.hexdigest()


def tensor_hash(tensor) -> str:
    digest = hashlib.sha256()
    data = memoryview(tensor.data).cast("B")
    for offset in range(0, len(data), CHUNK_SIZE):
        digest.update(data[offset:offset + CHUNK_SIZE])
    return digest.hexdigest()


def metadata(reader) -> dict:
    return {
        name: field.contents()
        for name, field in reader.fields.items()
        if not name.startswith("GGUF.")
    }


def normalized_shape(shape) -> tuple[int, ...]:
    values = [int(n) for n in shape]
    # GGML tensor descriptors pad the unused trailing dimensions with ones.
    while len(values) > 1 and values[-1] == 1:
        values.pop()
    return tuple(values)


def head_shapes(values: dict) -> dict[str, tuple[int, ...]]:
    embd = int(values[ARCH + ".embedding_length"])
    heads = int(values[ARCH + ".attention.head_count"])
    kv_heads = int(values[ARCH + ".attention.head_count_kv"])
    key = int(values[ARCH + ".attention.key_length"])
    val = int(values[ARCH + ".attention.value_length"])
    experts = int(values[ARCH + ".expert_count"])
    ff = int(values[ARCH + ".expert_feed_forward_length"])
    shared = int(values[ARCH + ".expert_shared_feed_forward_length"])
    return {
        "attn_norm.weight": (embd,),
        "post_attention_norm.weight": (embd,),
        "attn_q.weight": (embd, heads * key * 2),
        "attn_k.weight": (embd, kv_heads * key),
        "attn_v.weight": (embd, kv_heads * val),
        "attn_qkv.weight": (embd, heads * key * 2 + kv_heads * (key + val)),
        "attn_output.weight": (heads * val, embd),
        "attn_q_norm.weight": (key,),
        "attn_k_norm.weight": (key,),
        "ffn_gate_inp.weight": (embd, experts),
        "ffn_down_exps.weight": (ff, embd, experts),
        "ffn_gate_exps.weight": (embd, ff, experts),
        "ffn_up_exps.weight": (embd, ff, experts),
        "ffn_gate_up_exps.weight": (embd, 2 * ff, experts),
        "ffn_gate_inp_shexp.weight": (embd,),
        "ffn_gate_shexp.weight": (embd, shared),
        "ffn_up_shexp.weight": (embd, shared),
        "ffn_down_shexp.weight": (shared, embd),
        "nextn.eh_proj.weight": (2 * embd, embd),
        "nextn.enorm.weight": (embd,),
        "nextn.hnorm.weight": (embd,),
    }


def validate_head(base, head) -> list:
    target, source = metadata(base), metadata(head)
    if target.get("general.architecture") != ARCH or source.get("general.architecture") != ARCH:
        raise ValueError("only matching qwen35moe architectures are supported")
    if target.get(BLOCK_COUNT) != 40 or target.get(NEXTN, 0) != 0:
        raise ValueError("expected a 40-block Cerebellum model without an MTP head")
    if source.get(BLOCK_COUNT) != 41 or source.get(NEXTN) != 1:
        raise ValueError("expected source block_count=41 and nextn_predict_layers=1")
    required_source_keys = (
        "embedding_length", "attention.head_count", "attention.head_count_kv",
        "attention.key_length", "attention.value_length", "expert_count",
        "rope.freq_base", "rope.dimension_count",
        "attention.layer_norm_rms_epsilon",
    )
    for suffix in required_source_keys:
        if ARCH + "." + suffix not in source:
            raise ValueError(f"head is missing required metadata: {suffix}")
    # A head-only GGUF omits trunk-specific metadata. Match all available
    # structural values and validate missing dimensions against its tensors.
    for key, value in target.items():
        if key.startswith(ARCH + ".") and key not in (BLOCK_COUNT, NEXTN):
            if key in source and source[key] != value:
                raise ValueError(f"incompatible structural metadata: {key}")
        if key.startswith("tokenizer.ggml.") and key in source and source[key] != value:
            raise ValueError(f"incompatible tokenizer metadata: {key}")
    prefix = "blk.40."
    extra = [tensor for tensor in head.tensors if tensor.name.startswith(prefix)]
    names = [tensor.name for tensor in extra]
    if len(set(names)) != len(names):
        raise ValueError("duplicate head tensors")
    expected = head_shapes(target)
    optional = {
        "attn_qkv.weight", "attn_q.weight", "attn_k.weight", "attn_v.weight",
        "ffn_gate_up_exps.weight", "ffn_gate_exps.weight", "ffn_up_exps.weight",
    }
    vocab = next(t for t in base.tensors if t.name == "token_embd.weight").shape[1]
    embd = target[ARCH + ".embedding_length"]
    expected.update({
        "nextn.embed_tokens.weight": (embd, int(vocab)),
        "nextn.shared_head_head.weight": (embd, int(vocab)),
        "nextn.shared_head_norm.weight": (embd,),
    })
    optional.update({
        "nextn.embed_tokens.weight", "nextn.shared_head_head.weight",
        "nextn.shared_head_norm.weight",
    })
    present = {tensor.name.removeprefix(prefix) for tensor in extra}
    missing = expected.keys() - optional - present
    if missing:
        raise ValueError(f"missing MTP tensors: {sorted(missing)}")
    for alternatives in (
        ({"attn_qkv.weight"}, {"attn_q.weight", "attn_k.weight", "attn_v.weight"}),
        ({"ffn_gate_up_exps.weight"}, {"ffn_gate_exps.weight", "ffn_up_exps.weight"}),
    ):
        if sum(group <= present for group in alternatives) != 1:
            raise ValueError("head must have exactly one supported projection layout")
    for tensor in extra:
        suffix = tensor.name.removeprefix(prefix)
        if suffix not in expected or normalized_shape(tensor.shape) != normalized_shape(expected[suffix]):
            raise ValueError(f"unexpected MTP tensor shape: {tensor.name} {tensor.shape}")
    if any(t.name.startswith(prefix) for t in base.tensors):
        raise ValueError("target already contains block 40 tensors")
    return extra


def verify_merged(base, head_tensors: list, merged) -> dict:
    target_values = metadata(base)
    expected_values = {**target_values, BLOCK_COUNT: 41, NEXTN: 1}
    if metadata(merged) != expected_values:
        raise ValueError("merged metadata differs outside the two MTP keys")
    expected = list(base.tensors) + head_tensors
    actual = {tensor.name: tensor for tensor in merged.tensors}
    if len(actual) != len(merged.tensors) or set(actual) != {t.name for t in expected}:
        raise ValueError("merged tensor inventory differs")
    manifest = []
    for tensor in expected:
        result = actual[tensor.name]
        if tuple(result.shape) != tuple(tensor.shape) or result.tensor_type != tensor.tensor_type:
            raise ValueError(f"changed tensor descriptor: {tensor.name}")
        original_hash = tensor_hash(tensor)
        if tensor_hash(result) != original_hash:
            raise ValueError(f"changed tensor bytes: {tensor.name}")
        manifest.append({
            "name": tensor.name,
            "shape": [int(n) for n in tensor.shape],
            "type": tensor.tensor_type.name,
            "sha256": original_hash,
        })
    return {
        "verified_main_tensors": len(base.tensors),
        "verified_head_tensors": len(head_tensors),
        "tensors": manifest,
    }


def merge(gguf, base_path: Path, head_path: Path, output: Path) -> dict:
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError("output or partial output already exists")
    base = gguf.GGUFReader(str(base_path), "r")
    head = gguf.GGUFReader(str(head_path), "r")
    if base.byte_order != "I" or head.byte_order != "I":
        raise ValueError("this deployment requires native little-endian GGUF")
    extra = validate_head(base, head)
    tensors = list(base.tensors) + extra
    if shutil.disk_usage(output.parent).free < sum(t.data.nbytes for t in tensors) + 64 * 1024**2:
        raise ValueError("insufficient free space for a separate merged GGUF")
    original_sha = file_hash(base_path)
    writer = gguf.GGUFWriter(str(partial), ARCH)
    try:
        for key, field in base.fields.items():
            if key.startswith("GGUF.") or key in ("general.architecture", BLOCK_COUNT, NEXTN):
                continue
            writer.add_key_value(
                key, field.contents(), field.types[0],
                field.types[1] if len(field.types) > 1 else None,
            )
        writer.add_key_value(BLOCK_COUNT, 41, gguf.GGUFValueType.UINT32)
        writer.add_key_value(NEXTN, 1, gguf.GGUFValueType.UINT32)
        writer.data_alignment = int(metadata(base).get("general.alignment", 32))
        for tensor in tensors:
            writer.add_tensor_info(
                tensor.name, tensor.data.shape, tensor.data.dtype,
                tensor.data.nbytes, raw_dtype=tensor.tensor_type,
            )
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_ti_data_to_file()
        for index, tensor in enumerate(tensors, 1):
            writer.write_tensor_data(tensor.data)
            if index % 100 == 0 or index == len(tensors):
                print(f"copied {index}/{len(tensors)} tensors", flush=True)
    finally:
        writer.close()
    verified = verify_merged(base, extra, gguf.GGUFReader(str(partial), "r"))
    if file_hash(base_path) != original_sha:
        raise ValueError("original model changed during preparation")
    result = {
        "base_sha256": original_sha,
        "merged_sha256": file_hash(partial),
        "merged_bytes": partial.stat().st_size,
        **verified,
    }
    # A hard link publishes without replacing a pre-existing destination.
    os.link(partial, output)
    partial.unlink()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--gguf-python-path", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    report_path = Path(args.report).resolve()
    if report_path.exists():
        parser.error("report already exists")
    report = {
        "passed": False,
        "source": {
            "repository": HEAD_REPOSITORY, "revision": HEAD_REVISION,
            "filename": HEAD_FILENAME, "sha256": HEAD_SHA256,
        },
    }
    try:
        base, head, output = (Path(value).resolve() for value in (args.base, args.head, args.output))
        if len({base, head, output, report_path}) != 4:
            raise ValueError("input, output and report paths must be distinct")
        actual_sha = file_hash(head)
        if actual_sha != HEAD_SHA256:
            raise ValueError(f"head SHA256 mismatch: {actual_sha}")
        sys.path.insert(0, str(Path(args.gguf_python_path).resolve()))
        gguf = importlib.import_module("gguf")
        report.update({"base": str(base), "head": str(head), "output": str(output)})
        report.update(merge(gguf, base, head, output))
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    save(report_path, report)
    print(json.dumps({k: v for k, v in report.items() if k != "tensors"}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
