#!/usr/bin/env python3
"""Resolve the persisted LMCache settings for the V100 TP2 services."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_router.lmcache_runtime import (  # noqa: E402
    build_kv_transfer_config,
    load_lmcache_settings,
    server_host_port,
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
    parser.add_argument(
        "--format",
        choices=("lines", "json"),
        default="lines",
    )
    parser.add_argument("--server-url")
    args = parser.parse_args()

    value = load_lmcache_settings(args.defaults, args.runtime)
    if args.server_url:
        value["server_url"] = args.server_url
    host, port = server_host_port(str(value["server_url"]))
    resolved = {
        **value,
        "server_host": host,
        "server_port": port,
        "kv_transfer_config": build_kv_transfer_config(
            str(value["server_url"])
        ),
    }
    if args.format == "json":
        import json

        print(
            json.dumps(
                resolved,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0

    values = (
        "1" if resolved["enabled"] else "0",
        str(resolved["server_url"]),
        str(resolved["http_url"]),
        str(resolved["server_host"]),
        str(resolved["server_port"]),
        str(resolved["l1_size_gb"]),
        str(resolved["memory_max_gb"]),
        str(resolved["chunk_size"]),
        str(resolved["numa_node"]),
        "1" if resolved["separate_object_groups"] else "0",
        str(resolved["kv_transfer_config"]),
    )
    if any("\n" in item or "\r" in item for item in values):
        raise ValueError("resolved LMCache values must be single-line")
    print("\n".join(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
