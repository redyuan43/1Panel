#!/usr/bin/env python3
"""Patch the pinned Edge vLLM image for safe Qwen3.8 disk KV offload."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import sys


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one source match in {path}, found {count}"
        )
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def patch_manager(path: Path) -> None:
    replace_once(
        path,
        """        self.num_cpu_blocks = self.cpu_kv_cache_config.num_blocks
        # Find the full attention kv group for prefix cache matching.
""",
        """        self.num_cpu_blocks = self.cpu_kv_cache_config.num_blocks
        # QSA CircularBufferSpec is mutable per-request scratch state. It has
        # no block hashes and must never participate in external prefix-cache
        # store or load decisions.
        self._offload_group_ids = frozenset(
            group_id
            for group_id, group in enumerate(kv_cache_config.kv_cache_groups)
            if group.kv_cache_spec.prefix_cacheable
        )
        # Find the full attention kv group for prefix cache matching.
""",
        "manager offload group projection",
    )
    replace_once(
        path,
        """        for g in kv_cache_config.kv_cache_groups:
            spec = g.kv_cache_spec
            block_size = spec.block_size * cp_world_size
""",
        """        for g in kv_cache_config.kv_cache_groups:
            spec = g.kv_cache_spec
            if not spec.prefix_cacheable:
                continue
            block_size = spec.block_size * cp_world_size
""",
        "manager lazy target exclusion",
    )
    replace_once(
        path,
        """        cpu_hit_blocks: list[list[KVCacheBlock]] = []
        for g in range(num_groups):
            g_block_size = (
""",
        """        cpu_hit_blocks: list[list[KVCacheBlock]] = []
        for g in range(num_groups):
            if g not in self._offload_group_ids:
                cpu_hit_blocks.append([])
                continue
            g_block_size = (
""",
        "manager load group exclusion",
    )
    replace_once(
        path,
        """            for g in range(num_groups):
                # FIXME (yifan): handle CPU cache eviction, where
""",
        """            for g in range(num_groups):
                if g not in self._offload_group_ids:
                    continue
                # FIXME (yifan): handle CPU cache eviction, where
""",
        "manager eager store group exclusion",
    )


def patch_worker(path: Path) -> None:
    replace_once(
        path,
        """        # Compute-done event recorded before each store; reused across steps
        # (get_finished runs once per step, copy queue is FIFO).
        self._store_compute_done: torch.Event | None = None
""",
        """        # Compute-done event shared by load and store barriers. A load may
        # target a GPU block that the previous request is still reading on the
        # compute stream, so both transfer directions wait for this event.
        self._compute_done: torch.Event | None = None
""",
        "worker shared compute event",
    )
    replace_once(
        path,
        """        Stores (GPU->CPU) read the live KV cache, which the compute stream may
        still be writing under v1 overlapped execution, so they are ordered
        after a compute-done event recorded on the current stream. Loads
        (CPU->GPU) read stable pinned host memory and launch immediately. See
        #45704 for the bug and #39306 for the srcAccessOrder rationale.
""",
        """        Both transfer directions wait for a compute-done event. Stores must
        not read KV blocks before compute finishes writing them, and loads must
        not overwrite a reused GPU block while the previous request is still
        reading it. See vLLM issues #45704 and #47282.
""",
        "worker transfer ordering documentation",
    )
    replace_once(
        path,
        """        if metadata is not None:
            backend = self._backend
            assert backend is not None
            if metadata.load_cpu_blocks:
                backend.launch_copy(
                    metadata.load_cpu_blocks,
                    metadata.load_gpu_blocks,
                    is_store=False,
                    event_idx=metadata.load_event,
                    events_list=self._load_events,
                )
            if metadata.store_gpu_blocks:
                if self._store_compute_done is None:
                    self._store_compute_done = torch.Event()
                self._store_compute_done.record(torch.cuda.current_stream())
                backend.launch_copy(
                    metadata.store_gpu_blocks,
                    metadata.store_cpu_blocks,
                    is_store=True,
                    event_idx=metadata.store_event,
                    events_list=self._store_events,
                    wait_event=self._store_compute_done,
                )
""",
        """        if metadata is not None:
            backend = self._backend
            assert backend is not None
            has_load = bool(metadata.load_cpu_blocks)
            has_store = bool(metadata.store_gpu_blocks)
            if has_load or has_store:
                if self._compute_done is None:
                    self._compute_done = torch.Event()
                self._compute_done.record(torch.cuda.current_stream())
            if has_load:
                backend.launch_copy(
                    metadata.load_cpu_blocks,
                    metadata.load_gpu_blocks,
                    is_store=False,
                    event_idx=metadata.load_event,
                    events_list=self._load_events,
                    wait_event=self._compute_done,
                )
            if has_store:
                backend.launch_copy(
                    metadata.store_gpu_blocks,
                    metadata.store_cpu_blocks,
                    is_store=True,
                    event_idx=metadata.store_event,
                    events_list=self._store_events,
                    wait_event=self._compute_done,
                )
""",
        "worker symmetric load and store barrier",
    )


def verify(path: Path, markers: tuple[str, ...]) -> None:
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))
    for marker in markers:
        if marker not in source:
            raise RuntimeError(f"missing verification marker {marker!r} in {path}")
    print(f"{path}: {hashlib.sha256(source.encode()).hexdigest()}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_simple_kv_disk.py SITE_PACKAGES")
    root = Path(sys.argv[1])
    manager = root / "vllm/v1/simple_kv_offload/manager.py"
    worker = root / "vllm/v1/simple_kv_offload/worker.py"
    if not manager.is_file() or not worker.is_file():
        raise RuntimeError("pinned vLLM SimpleCPU offload sources are missing")

    patch_manager(manager)
    patch_worker(worker)
    verify(
        manager,
        (
            "self._offload_group_ids",
            "if not spec.prefix_cacheable:",
            "if g not in self._offload_group_ids:",
        ),
    )
    verify(
        worker,
        (
            "self._compute_done",
            "wait_event=self._compute_done",
            "vLLM issues #45704 and #47282",
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
