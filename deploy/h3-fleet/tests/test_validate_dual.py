from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_dual.py"
SPEC = importlib.util.spec_from_file_location("validate_dual", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def args(**values):
    defaults = {
        "width": 864,
        "height": 480,
        "length": 124,
        "steps": 6,
        "lora": "turbo.safetensors",
        "input_image": "",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_builds_turbo_workflow() -> None:
    workflow = MODULE.build_workflow(
        {
            "1": {"class_type": "UNETLoader", "inputs": {}},
            "2": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {}},
            "3": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {}},
            "4": {"class_type": "RandomNoise", "inputs": {}},
            "5": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {}},
            "6": {"class_type": "SaveVideo", "inputs": {}},
        },
        prompt="prompt",
        seed=123,
        index=0,
        args=args(),
    )
    assert workflow["2"]["inputs"]["lora_name"] == "turbo.safetensors"
    assert workflow["3"]["inputs"]["task_type"] == "T2VA"
    assert workflow["5"]["inputs"]["steps"] == 6


def test_builds_quality_workflow_without_lora() -> None:
    workflow = MODULE.build_workflow(
        {
            "1": {"class_type": "UNETLoader", "inputs": {}},
            "2": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {}},
            "3": {"class_type": "RandomNoise", "inputs": {}},
            "4": {"class_type": "BasicScheduler", "inputs": {}},
            "5": {"class_type": "SaveVideo", "inputs": {}},
        },
        prompt="prompt",
        seed=123,
        index=0,
        args=args(steps=14, lora=""),
    )
    assert workflow["2"]["inputs"]["length"] == 124
    assert workflow["4"]["inputs"]["steps"] == 14


def test_defines_three_distinct_validation_jobs() -> None:
    assert len(MODULE.PROMPTS) == 3
    assert len(MODULE.SEEDS) == 3
    assert len(set(MODULE.SEEDS)) == 3


def test_selects_an_explicit_seed_for_a_followup_job() -> None:
    assert MODULE.select_seeds(1, 2026090703) == [2026090703]
    assert MODULE.select_seeds(3, None) == MODULE.SEEDS


def test_builds_image_to_video_workflow() -> None:
    workflow = MODULE.build_workflow(
        {
            "1": {"class_type": "UNETLoader", "inputs": {}},
            "2": {"class_type": "LoadImage", "inputs": {}},
            "3": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {}},
            "4": {"class_type": "RandomNoise", "inputs": {}},
            "5": {"class_type": "BasicScheduler", "inputs": {}},
            "6": {"class_type": "SaveVideo", "inputs": {}},
        },
        prompt="custom prompt",
        seed=123,
        index=0,
        args=args(
            width=480,
            height=864,
            length=362,
            input_image="reference.png",
        ),
    )
    assert workflow["2"]["inputs"]["image"] == "reference.png"
    assert workflow["3"]["inputs"]["prompt"] == "custom prompt"
    assert workflow["3"]["inputs"]["width"] == 480
    assert workflow["3"]["inputs"]["height"] == 864
    assert workflow["3"]["inputs"]["length"] == 362


def test_custom_prompt_can_be_repeated_for_three_jobs(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("same prompt", encoding="utf-8")
    assert MODULE.load_prompts(3, prompt_file, None) == ["same prompt"] * 3


def test_loads_one_prompt_per_parallel_job(tmp_path: Path) -> None:
    prompt_files = []
    for index, prompt in enumerate(("lace", "dress", "bodysuit")):
        path = tmp_path / f"{index}.txt"
        path.write_text(prompt, encoding="utf-8")
        prompt_files.append(path)

    assert MODULE.load_prompts(3, None, prompt_files) == [
        "lace",
        "dress",
        "bodysuit",
    ]


def test_rejects_wrong_parallel_prompt_count(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("only one", encoding="utf-8")

    with pytest.raises(ValueError, match="count must match"):
        MODULE.load_prompts(3, None, [prompt_file])


def test_validates_expected_media_contract() -> None:
    result = MODULE.validate_media(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 864,
                    "height": 480,
                    "avg_frame_rate": "24/1",
                    "duration": "5.166667",
                    "nb_frames": "124",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "channels": 2,
                },
            ],
            "format": {"duration": "5.167"},
        },
        width=864,
        height=480,
        length=124,
    )
    assert result["ok"] is True
    assert result["frame_count"] == 124


def test_rejects_wrong_resolution() -> None:
    with pytest.raises(RuntimeError, match="unexpected resolution"):
        MODULE.validate_media(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 640,
                        "height": 480,
                        "avg_frame_rate": "24/1",
                        "duration": "5.166667",
                        "nb_frames": "124",
                    },
                    {"codec_type": "audio"},
                ]
            },
            width=864,
            height=480,
            length=124,
        )


def test_summarizes_metrics(tmp_path: Path) -> None:
    samples = [
        {
            "memory_available_bytes": 80,
            "swap_used_bytes": 2,
            "root_available_bytes": 30,
            "offload_available_bytes": 40,
            "gpus": [
                {
                    "index": 0,
                    "uuid": "gpu-0",
                    "memory_used_mib": 100,
                    "utilization_percent": 30,
                    "temperature_c": 60,
                }
            ],
            "processes": [
                {
                    "lane": "fast",
                    "rss_bytes": 10,
                    "pss_bytes": 8,
                }
            ],
        },
        {
            "memory_available_bytes": 70,
            "swap_used_bytes": 4,
            "root_available_bytes": 29,
            "offload_available_bytes": 39,
            "gpus": [
                {
                    "index": 0,
                    "uuid": "gpu-0",
                    "memory_used_mib": 200,
                    "utilization_percent": 90,
                    "temperature_c": 70,
                }
            ],
            "processes": [
                {
                    "lane": "fast",
                    "rss_bytes": 20,
                    "pss_bytes": 18,
                }
            ],
        },
    ]
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        "\n".join(MODULE.json.dumps(sample) for sample in samples),
        encoding="utf-8",
    )
    summary = MODULE.summarize_metrics(path)
    assert summary["min_memory_available_bytes"] == 70
    assert summary["max_swap_used_bytes"] == 4
    assert summary["gpus"]["gpu-0"]["max_memory_used_mib"] == 200
    assert summary["processes"]["fast"]["max_pss_bytes"] == 18


def test_wait_for_job_retries_transport_error(monkeypatch) -> None:
    prompt_id = "prompt-1"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                prompt_id: {
                    "outputs": {
                        "1": {
                            "videos": [
                                {
                                    "filename": "result.mp4",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            }

    class Client:
        attempts = 0

        async def get(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.RemoteProtocolError("transient disconnect")
            return Response()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(MODULE.asyncio, "sleep", no_sleep)
    client = Client()
    _, output = asyncio.run(
        MODULE.wait_for_job(client, "http://fleet", prompt_id, 30)
    )
    assert client.attempts == 2
    assert output["filename"] == "result.mp4"
