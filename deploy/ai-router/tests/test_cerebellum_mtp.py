from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare-cerebellum-mtp.py"
SPEC = importlib.util.spec_from_file_location("prepare_mtp", PATH)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def model_values():
    return {
        "general.architecture": "qwen35moe",
        "general.name": "test-main",
        "qwen35moe.block_count": 40,
        "qwen35moe.context_length": 262144,
        "qwen35moe.embedding_length": 32,
        "qwen35moe.attention.head_count": 4,
        "qwen35moe.attention.head_count_kv": 2,
        "qwen35moe.attention.key_length": 8,
        "qwen35moe.attention.value_length": 8,
        "qwen35moe.attention.layer_norm_rms_epsilon": 1e-5,
        "qwen35moe.expert_count": 2,
        "qwen35moe.expert_used_count": 1,
        "qwen35moe.expert_feed_forward_length": 32,
        "qwen35moe.expert_shared_feed_forward_length": 32,
        "qwen35moe.rope.freq_base": 10000000.0,
        "qwen35moe.rope.dimension_count": 8,
        "tokenizer.ggml.tokens": ["a", "b"],
        "tokenizer.chat_template": "the target template",
    }


def fake_reader(values, tensors):
    return SimpleNamespace(
        fields={key: SimpleNamespace(contents=lambda value=value: value) for key, value in values.items()},
        tensors=tensors,
    )


def models():
    values = model_values()
    base = fake_reader(values, [
        SimpleNamespace(name="token_embd.weight", shape=np.array([32, 2])),
    ])
    head_values = {**values, "qwen35moe.block_count": 41, "qwen35moe.nextn_predict_layers": 1}
    for name in (
        "qwen35moe.expert_used_count", "qwen35moe.expert_feed_forward_length",
        "qwen35moe.expert_shared_feed_forward_length", "tokenizer.ggml.tokens",
    ):
        head_values.pop(name)
    shapes = prepare.head_shapes(values)
    for name in ("attn_qkv.weight", "ffn_gate_up_exps.weight"):
        shapes.pop(name)
    shapes["ffn_gate_inp_shexp.weight"] = (32, 1)
    head = fake_reader(head_values, [
        SimpleNamespace(name="blk.40." + name, shape=np.array(shape))
        for name, shape in shapes.items()
    ])
    return base, head


def test_accept_head_only_metadata_and_padded_vector():
    base, head = models()
    assert len(prepare.validate_head(base, head)) == 19


@pytest.mark.parametrize("key,value", [
    ("general.architecture", "qwen35"),
    ("qwen35moe.embedding_length", 64),
    ("qwen35moe.expert_count", 3),
    ("qwen35moe.block_count", 40),
    ("qwen35moe.nextn_predict_layers", 2),
])
def test_reject_incompatible_heads(key, value):
    base, head = models()
    head.fields[key] = SimpleNamespace(contents=lambda: value)
    with pytest.raises(ValueError):
        prepare.validate_head(base, head)


def test_reject_missing_required_metadata_and_tensor():
    base, head = models()
    head.fields.pop("qwen35moe.embedding_length")
    with pytest.raises(ValueError, match="required metadata"):
        prepare.validate_head(base, head)
    base, head = models()
    head.tensors.pop()
    with pytest.raises(ValueError, match="missing MTP tensors"):
        prepare.validate_head(base, head)


def test_reject_invalid_projection_shape_and_duplicate():
    base, head = models()
    head.tensors[0].shape = np.array([3, 7])
    with pytest.raises(ValueError, match="shape"):
        prepare.validate_head(base, head)
    base, head = models()
    head.tensors.append(head.tensors[0])
    with pytest.raises(ValueError, match="duplicate"):
        prepare.validate_head(base, head)


def test_reports_cannot_overwrite_evidence(tmp_path):
    path = tmp_path / "report.json"
    prepare.save(path, {"passed": False})
    with pytest.raises(FileExistsError):
        prepare.save(path, {"passed": True})


def write_gguf(gguf, path, values, tensors):
    writer = gguf.GGUFWriter(str(path), "qwen35moe")
    for key, value in values.items():
        if key == "general.architecture":
            continue
        if isinstance(value, str):
            writer.add_key_value(key, value, gguf.GGUFValueType.STRING)
        elif isinstance(value, list):
            writer.add_key_value(key, value, gguf.GGUFValueType.ARRAY, gguf.GGUFValueType.STRING)
        elif isinstance(value, float):
            writer.add_key_value(key, value, gguf.GGUFValueType.FLOAT32)
        else:
            writer.add_key_value(key, value, gguf.GGUFValueType.INT32)
    for name, data, dtype in tensors:
        writer.add_tensor(name, data, raw_dtype=dtype)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def test_real_gguf_merge_preserves_quantized_data_and_metadata(tmp_path):
    gguf = pytest.importorskip("gguf")
    try:
        inspect.signature(gguf.GGUFWriter.add_key_value).bind(
            None, "test.array", ["value"], gguf.GGUFValueType.ARRAY,
            gguf.GGUFValueType.STRING,
        )
    except TypeError:
        pytest.skip("installed gguf lacks typed array metadata support")
    base, head = models()
    values = model_values()
    values["test.signed"] = -7
    base_path, head_path, output = (tmp_path / name for name in ("base.gguf", "head.gguf", "out.gguf"))
    quantized = gguf.quants.quantize(
        np.arange(64, dtype=np.float32).reshape(2, 32), gguf.GGMLQuantizationType.Q8_0,
    )
    write_gguf(gguf, base_path, values, [
        ("token_embd.weight", quantized, gguf.GGMLQuantizationType.Q8_0),
    ])
    write_gguf(gguf, head_path, prepare.metadata(head), [
        (t.name, np.ones(tuple(reversed(t.shape)), dtype=np.float32), None)
        for t in head.tensors
    ])
    original_hash = prepare.file_hash(base_path)
    report = prepare.merge(gguf, base_path, head_path, output)
    assert report["verified_main_tensors"] == 1
    assert report["verified_head_tensors"] == 19
    assert prepare.file_hash(base_path) == original_hash
    assert not output.with_name("out.gguf.partial").exists()
    merged = gguf.GGUFReader(str(output))
    assert merged.fields["test.signed"].contents() == -7
    assert merged.fields["tokenizer.chat_template"].contents() == "the target template"
    with pytest.raises(FileExistsError):
        prepare.merge(gguf, base_path, head_path, output)
