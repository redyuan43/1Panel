import asyncio
from pathlib import Path

import httpx
import yaml

from ai_router.fleet_adapter import (
    FleetMonitor,
    load_config,
)
from ai_router.store import InMemoryStateStore


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": "lab/model",
                "revision": "revision-1",
                "workers": [
                    {
                        "worker_id": "worker-a",
                        "api_base": "http://worker-a/v1",
                        "profile_id": "nx-q4-vision",
                        "tier": "edge-small",
                        "priority": 10,
                        "context_size": 57344,
                        "safe_context_tokens": 57344,
                        "cache_type_k": "q4_0",
                        "cache_type_v": "q4_0",
                        "context_checkpoints": 2,
                        "backend_api_key_env": "WORKER_A_KEY",
                        "expected_model_alias": "lab/model",
                        "expected_parameter_count": 35,
                        "expected_quantization": "IQ2_M",
                        "expected_model_sha256": "a" * 64,
                        "expected_projector_sha256": "b" * 64,
                        "modalities": ["text", "image"],
                        "vision_status": "validated",
                        "max_images": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _transport(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer secret"
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/slots":
        return httpx.Response(
            200,
            json=[
                {
                    "id": 0,
                    "id_task": 7,
                    "n_ctx": 57344,
                    "is_processing": False,
                }
            ],
        )
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "capabilities": [
                            "completion",
                            "multimodal",
                        ]
                    }
                ],
                "data": [
                    {
                        "id": "lab/model",
                        "aliases": ["lab/model"],
                        "created": 123,
                        "meta": {
                            "n_params": 35,
                            "ftype": "IQ2_M - 2.7 bpw",
                        },
                    }
                ],
            },
        )
    raise AssertionError(request.url)


def test_fleet_monitor_reports_validated_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_A_KEY", "secret")
    config = load_config(_config(tmp_path))
    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_transport)
        ) as client:
            return await FleetMonitor(config, client).health()

    payload = asyncio.run(run())
    assert payload["ok"] is True
    assert payload["ready_workers"] == 1
    worker = payload["workers"][0]
    assert worker["worker_id"] == "worker-a"
    assert worker["backend_api_key_env"] == "WORKER_A_KEY"
    assert worker["context_checkpoints"] == 2
    assert worker["expected_model_sha256"] == "a" * 64
    assert worker["expected_projector_sha256"] == "b" * 64
    assert (
        worker["artifact_verification"]
        == "deployment-preflight-required"
    )
    assert worker["config_drift"] == []
    assert worker["state"] == "available"


def test_fleet_monitor_rejects_missing_vision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_A_KEY", "secret")

    def no_vision(request: httpx.Request) -> httpx.Response:
        response = _transport(request)
        if request.url.path != "/v1/models":
            return response
        value = response.json()
        value["models"][0]["capabilities"] = ["completion"]
        return httpx.Response(200, json=value)

    config = load_config(_config(tmp_path))
    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(no_vision)
        ) as client:
            return await FleetMonitor(config, client).health()

    payload = asyncio.run(run())
    assert payload["ok"] is False
    assert payload["workers"][0]["config_drift"] == [
        "vision_capability"
    ]


def test_fleet_monitor_keeps_standby_worker_out_of_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_A_KEY", "secret")
    path = _config(tmp_path)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["workers"][0]["routing_enabled"] = False
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    config = load_config(path)

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_transport)
        ) as client:
            return await FleetMonitor(config, client).health()

    payload = asyncio.run(run())
    worker = payload["workers"][0]
    assert payload["ok"] is False
    assert payload["ready_workers"] == 0
    assert payload["available_workers"] == 0
    assert worker["routing_enabled"] is False
    assert worker["ready"] is False
    assert worker["state"] == "standby"
    assert worker["config_drift"] == []


def test_production_fleet_keeps_ivan_on_standby() -> None:
    config = load_config(
        Path(__file__).resolve().parents[1]
        / "config"
        / "qwen36-fleet.yaml"
    )
    workers = {
        worker.worker_id: worker
        for worker in config.workers
    }
    assert workers["qwen36-nx3"].routing_enabled is True
    assert workers["qwen36-nx4"].routing_enabled is True
    assert workers["qwen36-agx"].routing_enabled is True
    assert workers["qwen36-ivan-laptop"].routing_enabled is False


def test_fleet_monitor_persists_worker_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_A_KEY", "secret")
    config = load_config(_config(tmp_path))
    store = InMemoryStateStore()
    task_id = 9

    def transport(request: httpx.Request) -> httpx.Response:
        response = _transport(request)
        if request.url.path != "/slots":
            return response
        value = response.json()
        value[0]["id_task"] = task_id
        return httpx.Response(200, json=value)

    async def run() -> tuple[str, str]:
        nonlocal task_id
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            first = await FleetMonitor(
                config,
                client,
                store,
            ).health()
            task_id = 0
            second = await FleetMonitor(
                config,
                client,
                store,
            ).health()
        return (
            first["workers"][0]["cache_generation"],
            second["workers"][0]["cache_generation"],
        )

    before, after = asyncio.run(run())
    assert before != after


def test_fleet_monitor_ignores_ephemeral_model_created_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKER_A_KEY", "secret")
    config = load_config(_config(tmp_path))
    store = InMemoryStateStore()
    model_created = 123

    def transport(request: httpx.Request) -> httpx.Response:
        response = _transport(request)
        if request.url.path != "/v1/models":
            return response
        value = response.json()
        value["data"][0]["created"] = model_created
        return httpx.Response(200, json=value)

    async def run() -> tuple[str, str]:
        nonlocal model_created
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            monitor = FleetMonitor(config, client, store)
            first = await monitor.health()
            model_created = 456
            second = await monitor.health()
        return (
            first["workers"][0]["cache_generation"],
            second["workers"][0]["cache_generation"],
        )

    before, after = asyncio.run(run())
    assert before == after
