from __future__ import annotations

import math


def sampler_overlap(tasks: list[dict]) -> dict:
    events = []
    complete = bool(tasks)
    for index, task in enumerate(tasks):
        progress = task.get("sampler_progress") or {}
        intervals = progress.get("sampler_intervals", [])
        if progress.get("cached") or not intervals:
            complete = False
            continue
        previous_end = None
        for interval in intervals:
            start = interval.get("started_at")
            finish = interval.get("finished_at")
            if (type(start) not in (int, float) or type(finish) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(finish) or finish <= start
                    or previous_end is not None and start < previous_end):
                complete = False
                continue
            previous_end = finish
            events.extend(((start, 1, index), (finish, -1, index)))
    active = set()
    peak = 0
    for _, change, identifier in sorted(events):
        if change > 0:
            active.add(identifier)
        else:
            active.discard(identifier)
        peak = max(peak, len(active))
    return {"peak_parallel": peak, "parallel_validated": complete and peak >= 2
            and all(task.get("status") == "completed" and task.get("media_validation", {}).get("ok") for task in tasks),
            "sampling_evidence_complete": complete, "measurement": "owned non-cached sampler event overlap, not kernel occupancy"}
