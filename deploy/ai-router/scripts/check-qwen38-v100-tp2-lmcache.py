#!/usr/bin/env python3
"""Fail-closed LMCache readiness check used before the TP2 model starts."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_router.lmcache_runtime import (  # noqa: E402
    load_lmcache_settings,
    validate_lmcache_settings,
)


def unregister_stale_gpu_contexts(
    settings: dict[str, object],
) -> list[int]:
    status_url = str(settings["http_url"]).rstrip("/") + "/status"
    with urllib.request.urlopen(status_url, timeout=5) as response:
        payload = json.load(response)
    instance_ids = [
        int(value) for value in payload.get("registered_gpu_ids", [])
    ]
    if not instance_ids:
        return []

    container_name = os.environ.get(
        "CONTAINER_NAME",
        "qwen38-v100-tp2-vllm",
    )
    exists = subprocess.run(
        ["docker", "container", "inspect", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if exists:
        raise RuntimeError(
            "refusing to unregister LMCache workers while the vLLM "
            f"container exists: {container_name}"
        )

    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.environ["GPU_UUIDS"])
    import zmq
    from lmcache.v1.multiprocess.mq import MessageQueueClient
    from lmcache.v1.multiprocess.protocols.base import RequestType

    context = zmq.Context()
    client = MessageQueueClient(str(settings["server_url"]), context)
    try:
        for instance_id in instance_ids:
            client.submit_request(
                RequestType.UNREGISTER_KV_CACHE,
                [instance_id],
            ).result(timeout=30)
    finally:
        client.close()
        context.term()
    return instance_ids


def wait_for_gpu_memory(deadline: float) -> list[dict[str, int | str]]:
    gpu_uuids = [
        value.strip()
        for value in os.environ.get("GPU_UUIDS", "").split(",")
        if value.strip()
    ]
    if not gpu_uuids:
        raise ValueError("GPU_UUIDS is required for vLLM readiness")
    utilization = float(os.environ.get("GPU_MEMORY_UTILIZATION", "0.90"))
    if not 0 < utilization <= 1:
        raise ValueError("GPU_MEMORY_UTILIZATION must be in (0, 1]")

    snapshots: list[dict[str, int | str]] = []
    last_error = "GPU memory has not been checked"
    while time.monotonic() < deadline:
        snapshots = []
        try:
            for uuid in gpu_uuids:
                result = subprocess.run(
                    [
                        "nvidia-smi",
                        f"--id={uuid}",
                        "--query-gpu=memory.free,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                values = [
                    int(value.strip())
                    for value in result.stdout.strip().split(",")
                ]
                if len(values) != 2:
                    raise RuntimeError(
                        f"unexpected nvidia-smi output for {uuid}"
                    )
                free_mib, total_mib = values
                required_mib = math.ceil(total_mib * utilization)
                snapshots.append(
                    {
                        "uuid": uuid,
                        "free_mib": free_mib,
                        "total_mib": total_mib,
                        "required_mib": required_mib,
                    }
                )
            if all(
                int(item["free_mib"]) >= int(item["required_mib"])
                for item in snapshots
            ):
                return snapshots
            last_error = json.dumps(
                snapshots,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            ValueError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise RuntimeError(
        "GPU memory did not become ready before timeout: " + last_error
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--defaults",
        type=Path,
        default=ROOT / "config/defaults.yaml",
    )
    parser.add_argument(
        "--runtime",
        type=Path,
        default=Path("/opt/1panel/ai-router/settings.yaml"),
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--require-vllm-guards", action="store_true")
    args = parser.parse_args()

    settings = load_lmcache_settings(args.defaults, args.runtime)
    if "LMCACHE_ENABLED" in os.environ:
        raw_enabled = os.environ["LMCACHE_ENABLED"]
        if raw_enabled not in {"0", "1"}:
            raise ValueError("LMCACHE_ENABLED must be 0 or 1")
        settings["enabled"] = raw_enabled == "1"
    if os.environ.get("LMCACHE_HTTP_URL"):
        settings["http_url"] = os.environ["LMCACHE_HTTP_URL"]
    if os.environ.get("LMCACHE_L1_SIZE_GB"):
        settings["l1_size_gb"] = int(os.environ["LMCACHE_L1_SIZE_GB"])
    if os.environ.get("LMCACHE_CHUNK_SIZE"):
        settings["chunk_size"] = int(os.environ["LMCACHE_CHUNK_SIZE"])
    validate_lmcache_settings(settings)
    deadline = time.monotonic() + args.timeout
    gpu_memory: list[dict[str, int | str]] = []
    unregistered_instances: list[int] = []
    if args.require_vllm_guards:
        if os.environ.get("DISABLE_CUSTOM_ALL_REDUCE") != "1":
            raise ValueError(
                "LMCache requires DISABLE_CUSTOM_ALL_REDUCE=1"
            )
        if os.environ.get("NCCL_P2P_DISABLE") != "1":
            raise ValueError("LMCache requires NCCL_P2P_DISABLE=1")
        if settings["enabled"]:
            unregistered_instances = unregister_stale_gpu_contexts(
                settings
            )
        gpu_memory = wait_for_gpu_memory(deadline)
    if not settings["enabled"]:
        print(
            json.dumps(
                {
                    "healthy": True,
                    "lmcache_enabled": False,
                    "gpu_memory": gpu_memory,
                    "unregistered_instances": unregistered_instances,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0

    expected_bytes = int(settings["l1_size_gb"]) * (1 << 30)
    status_url = str(settings["http_url"]).rstrip("/") + "/status"
    last_error = "not started"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=5) as response:
                payload = json.load(response)
            storage = payload.get("storage_manager", {})
            l1 = storage.get("l1_manager", {})
            actual_bytes = int(l1.get("memory_total_bytes", 0))
            if payload.get("is_healthy") is not True:
                raise RuntimeError("LMCache reports unhealthy")
            if int(payload.get("chunk_size", 0)) != int(
                settings["chunk_size"]
            ):
                raise RuntimeError("LMCache chunk size does not match")
            if actual_bytes != expected_bytes:
                raise RuntimeError(
                    "LMCache L1 capacity does not match: "
                    f"{actual_bytes} != {expected_bytes}"
                )
            if payload.get("storage_manager", {}).get(
                "num_l2_adapters",
                0,
            ) != 0:
                raise RuntimeError("LMCache L2 storage must remain disabled")
            print(
                json.dumps(
                    {
                        "healthy": True,
                        "chunk_size": payload["chunk_size"],
                        "gpu_memory": gpu_memory,
                        "memory_total_bytes": actual_bytes,
                        "l2_adapters": 0,
                        "unregistered_instances": unregistered_instances,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            urllib.error.URLError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
    print(
        f"LMCache readiness failed after {args.timeout:.0f}s: {last_error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
