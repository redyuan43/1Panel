from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import yaml

from .store import RedisStateStore, StateStore


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "qwen36-fleet.yaml"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    api_base: str
    profile_id: str
    tier: str
    priority: int
    routing_enabled: bool
    context_size: int
    safe_context_tokens: int
    cache_type_k: str
    cache_type_v: str
    context_checkpoints: int
    prefill_tokens_per_second: float
    backend_api_key_env: str
    expected_model_alias: str
    expected_parameter_count: int
    expected_quantization: str
    expected_model_sha256: str
    expected_projector_sha256: str
    modalities: tuple[str, ...]
    vision_status: str
    max_images: int | None
    names: tuple[str, ...]


@dataclass(frozen=True)
class FleetConfig:
    model: str
    revision: str
    workers: tuple[WorkerSpec, ...]


def load_config(path: str | Path) -> FleetConfig:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    workers = tuple(
        _worker_spec_from_dict(item)
        for item in value.get("workers", [])
    )
    if not workers:
        raise ValueError("fleet config requires at least one worker")
    worker_ids = [item.worker_id for item in workers]
    if len(worker_ids) != len(set(worker_ids)):
        raise ValueError("fleet worker IDs must be unique")
    return FleetConfig(
        model=str(value["model"]),
        revision=str(value["revision"]),
        workers=workers,
    )


def _worker_spec_from_dict(item: dict[str, Any]) -> WorkerSpec:
    model_sha256 = str(item["expected_model_sha256"]).lower()
    projector_sha256 = str(
        item["expected_projector_sha256"]
    ).lower()
    if not _SHA256.fullmatch(model_sha256):
        raise ValueError("expected_model_sha256 must be a SHA-256")
    if not _SHA256.fullmatch(projector_sha256):
        raise ValueError(
            "expected_projector_sha256 must be a SHA-256"
        )
    return WorkerSpec(
            worker_id=str(item["worker_id"]),
            api_base=str(item["api_base"]).rstrip("/"),
            profile_id=str(item["profile_id"]),
            tier=str(item["tier"]),
            priority=int(item["priority"]),
            routing_enabled=bool(item.get("routing_enabled", True)),
            context_size=int(item["context_size"]),
            safe_context_tokens=int(item["safe_context_tokens"]),
            cache_type_k=str(item["cache_type_k"]),
            cache_type_v=str(item["cache_type_v"]),
            context_checkpoints=int(item["context_checkpoints"]),
            prefill_tokens_per_second=float(
                item.get("prefill_tokens_per_second", 1.0)
            ),
            backend_api_key_env=str(
                item.get("backend_api_key_env", "")
            ),
            expected_model_alias=str(item["expected_model_alias"]),
            expected_parameter_count=int(
                item["expected_parameter_count"]
            ),
            expected_quantization=str(item["expected_quantization"]),
            expected_model_sha256=model_sha256,
            expected_projector_sha256=projector_sha256,
            modalities=tuple(str(value) for value in item["modalities"]),
            vision_status=str(item["vision_status"]),
            max_images=(
                int(item["max_images"])
                if item.get("max_images") is not None
                else None
            ),
            names=tuple(str(value) for value in item.get("names", [])),
        )


class FleetMonitor:
    def __init__(
        self,
        config: FleetConfig,
        client: httpx.AsyncClient,
        store: StateStore | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self._epochs: dict[str, int] = {}
        self._last_task_ids: dict[str, int] = {}

    async def health(self) -> dict[str, Any]:
        workers = await asyncio.gather(
            *(self._probe(item) for item in self.config.workers)
        )
        ready = [item for item in workers if item["ready"]]
        available = [
            item
            for item in ready
            if item["state"] == "available"
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "revision": self.config.revision,
                    "workers": [
                        {
                            "worker_id": item["worker_id"],
                            "runtime_fingerprint": item[
                                "runtime_fingerprint"
                            ],
                            "config_drift": item["config_drift"],
                        }
                        for item in workers
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "ok": bool(ready),
            "model": self.config.model,
            "runtime_fingerprint": fingerprint,
            "worker_count": len(workers),
            "ready_workers": len(ready),
            "available_workers": len(available),
            "available_worker_ids": [
                item["worker_id"] for item in available
            ],
            "workers": workers,
        }

    async def _probe(self, spec: WorkerSpec) -> dict[str, Any]:
        headers = {}
        api_key = (
            os.environ.get(spec.backend_api_key_env, "")
            if spec.backend_api_key_env
            else ""
        )
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        root = (
            spec.api_base[:-3].rstrip("/")
            if spec.api_base.endswith("/v1")
            else spec.api_base
        )
        try:
            health, slots_response, models_response = await asyncio.gather(
                self.client.get(f"{root}/health", headers=headers),
                self.client.get(f"{root}/slots", headers=headers),
                self.client.get(
                    f"{spec.api_base}/models",
                    headers=headers,
                ),
            )
            health.raise_for_status()
            slots_response.raise_for_status()
            models_response.raise_for_status()
            slots = slots_response.json()
            models = models_response.json()
            return await self._worker_payload(spec, slots, models)
        except Exception as exc:
            return self._offline_payload(spec, exc)

    async def _worker_payload(
        self,
        spec: WorkerSpec,
        slots: Any,
        models: Any,
    ) -> dict[str, Any]:
        if not isinstance(slots, list) or not slots:
            raise ValueError("worker returned no slots")
        model_values = (
            models.get("data", [])
            if isinstance(models, dict)
            else []
        )
        if not model_values or not isinstance(model_values[0], dict):
            raise ValueError("worker returned no model metadata")
        model = model_values[0]
        meta = model.get("meta", {})
        meta = meta if isinstance(meta, dict) else {}
        capabilities = {
            str(item)
            for item in (
                models.get("models", [{}])[0].get("capabilities", [])
                if isinstance(models, dict)
                and isinstance(models.get("models"), list)
                and models["models"]
                and isinstance(models["models"][0], dict)
                else []
            )
        }
        actual_aliases = {
            str(model.get("id", "")),
            *(
                str(item)
                for item in model.get("aliases", [])
                if item
            ),
        }
        actual_context = max(
            int(item.get("n_ctx", 0))
            for item in slots
            if isinstance(item, dict)
        )
        drift: list[str] = []
        if spec.expected_model_alias not in actual_aliases:
            drift.append("model_alias")
        if (
            int(meta.get("n_params", 0))
            != spec.expected_parameter_count
        ):
            drift.append("parameter_count")
        if spec.expected_quantization not in str(meta.get("ftype", "")):
            drift.append("quantization")
        if actual_context != spec.context_size:
            drift.append("context_size")
        if (
            "image" in spec.modalities
            and "multimodal" not in capabilities
        ):
            drift.append("vision_capability")
        reported_model_sha256 = str(
            model.get("sha256")
            or meta.get("sha256")
            or ""
        ).lower()
        reported_projector_sha256 = str(
            models.get("projector_sha256", "")
            if isinstance(models, dict)
            else ""
        ).lower()
        if (
            reported_model_sha256
            and reported_model_sha256
            != spec.expected_model_sha256
        ):
            drift.append("model_sha256")
        if (
            reported_projector_sha256
            and reported_projector_sha256
            != spec.expected_projector_sha256
        ):
            drift.append("projector_sha256")

        processing = any(
            bool(item.get("is_processing"))
            for item in slots
            if isinstance(item, dict)
        )
        max_task_id = max(
            (
                int(item.get("id_task", 0))
                for item in slots
                if isinstance(item, dict)
            ),
            default=0,
        )
        state_key = (
            "router:qwen36-fleet:generation:"
            f"{spec.worker_id}"
        )
        persisted = (
            await self.store.get_json(state_key)
            if self.store is not None
            else None
        )
        epoch = int(
            (persisted or {}).get(
                "epoch",
                self._epochs.get(spec.worker_id, 1),
            )
        )
        previous_task_id = int(
            (persisted or {}).get(
                "max_task_id",
                self._last_task_ids.get(
                    spec.worker_id,
                    max_task_id,
                ),
            )
        )
        if max_task_id < previous_task_id:
            epoch += 1
        self._epochs[spec.worker_id] = epoch
        self._last_task_ids[spec.worker_id] = max_task_id
        if self.store is not None:
            await self.store.set_json(
                state_key,
                {
                    "epoch": epoch,
                    "max_task_id": max_task_id,
                },
            )
        generation_input = {
            "worker_id": spec.worker_id,
            "epoch": epoch,
            "model": sorted(actual_aliases),
            "model_meta": {
                key: meta.get(key)
                for key in (
                    "n_params",
                    "ftype",
                    "n_ctx_train",
                    "model_size",
                )
                if key in meta
            },
            "model_sha256": spec.expected_model_sha256,
            "projector_sha256": spec.expected_projector_sha256,
            "context": actual_context,
            "revision": self.config.revision,
        }
        cache_generation = hashlib.sha256(
            json.dumps(
                generation_input,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "worker_id": spec.worker_id,
            "api_base": spec.api_base,
            "profile_id": spec.profile_id,
            "tier": spec.tier,
            "priority": spec.priority,
            "routing_enabled": spec.routing_enabled,
            "gpu_ids": [],
            "gpu_uuids": [],
            "names": list(spec.names),
            "port": None,
            "context_size": actual_context,
            "safe_context_tokens": spec.safe_context_tokens,
            "cache_type_k": spec.cache_type_k,
            "cache_type_v": spec.cache_type_v,
            "context_checkpoints": spec.context_checkpoints,
            "prefill_tokens_per_second": (
                spec.prefill_tokens_per_second
            ),
            "modalities": list(spec.modalities),
            "vision_status": spec.vision_status,
            "max_images": spec.max_images,
            "backend_api_key_env": spec.backend_api_key_env,
            "expected_model_sha256": spec.expected_model_sha256,
            "expected_projector_sha256": (
                spec.expected_projector_sha256
            ),
            "artifact_verification": (
                "runtime-reported"
                if (
                    reported_model_sha256
                    and reported_projector_sha256
                )
                else "deployment-preflight-required"
            ),
            "runtime_fingerprint": cache_generation,
            "cache_generation": cache_generation,
            "ready": not drift and spec.routing_enabled,
            "state": (
                "standby"
                if not spec.routing_enabled
                else ("busy" if processing else "available")
            ),
            "config_drift": drift,
        }

    @staticmethod
    def _offline_payload(
        spec: WorkerSpec,
        exc: Exception,
    ) -> dict[str, Any]:
        return {
            "worker_id": spec.worker_id,
            "api_base": spec.api_base,
            "profile_id": spec.profile_id,
            "tier": spec.tier,
            "priority": spec.priority,
            "routing_enabled": spec.routing_enabled,
            "gpu_ids": [],
            "gpu_uuids": [],
            "names": list(spec.names),
            "port": None,
            "context_size": spec.context_size,
            "safe_context_tokens": spec.safe_context_tokens,
            "cache_type_k": spec.cache_type_k,
            "cache_type_v": spec.cache_type_v,
            "context_checkpoints": spec.context_checkpoints,
            "prefill_tokens_per_second": (
                spec.prefill_tokens_per_second
            ),
            "modalities": list(spec.modalities),
            "vision_status": spec.vision_status,
            "max_images": spec.max_images,
            "backend_api_key_env": spec.backend_api_key_env,
            "expected_model_sha256": spec.expected_model_sha256,
            "expected_projector_sha256": (
                spec.expected_projector_sha256
            ),
            "artifact_verification": "unavailable",
            "runtime_fingerprint": "",
            "cache_generation": "",
            "ready": False,
            "state": "offline",
            "config_drift": ["unreachable"],
            "error_code": type(exc).__name__,
        }


def create_app(
    monitor: FleetMonitor | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if monitor is not None:
            app.state.monitor = monitor
            yield
            return
        config_path = os.environ.get(
            "AI_ROUTER_QWEN36_FLEET_CONFIG",
            str(DEFAULT_CONFIG_PATH),
        )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=15.0),
        )
        store = RedisStateStore(
            os.environ["AI_ROUTER_REDIS_URL"]
        )
        app.state.monitor = FleetMonitor(
            load_config(config_path),
            client,
            store,
        )
        try:
            yield
        finally:
            await client.aclose()
            await store.close()

    app = FastAPI(
        title="Qwen3.6 Fleet Adapter",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        payload = await app.state.monitor.health()
        return JSONResponse(
            payload,
            status_code=200 if payload["ok"] else 503,
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        value = app.state.monitor.config.model
        return {
            "object": "list",
            "data": [{"id": value, "object": "model"}],
        }

    return app


app = create_app()
