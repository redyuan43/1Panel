#!/usr/bin/env python3
"""Minimal two-rank NCCL health check for the V100 TP2 pair."""

from __future__ import annotations

from datetime import timedelta
import json
import os

import torch
import torch.distributed as dist


def main() -> None:
    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    dist.init_process_group(
        backend="nccl",
        timeout=timedelta(seconds=10),
    )
    value = torch.tensor([float(rank + 1)], device=f"cuda:{local_rank}")
    dist.all_reduce(value)
    torch.cuda.synchronize()
    print(
        json.dumps(
            {
                "device": torch.cuda.get_device_name(local_rank),
                "local_rank": local_rank,
                "rank": rank,
                "sum": value.item(),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
