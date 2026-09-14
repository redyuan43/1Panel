"""Pure experimental working-set accounting; no collection or runtime changes.

Input: {"cgroup_current_bytes": integer bytes, "memory_stat": parsed memory.stat}.
memory_current remains an accepted alias; supplying both requires equality.
Seven required stat fields plus memory_current form the eight required values.
Additional stat counters (including workingset/pgscan/pgsteal) are preserved.
All counters are nonnegative integers, never strings, floats or booleans.
ValueError means unverifiable admission evidence, not cancellation of running
work. An allow result covers ONLY this calculation; the caller retains all
lease, telemetry, physical-memory, swap, PSI, GPU and hard-limit checks.
Nothing here reclaims cache or certifies that discounted bytes can be freed.
"""
from __future__ import annotations

import math


GIB = 1024**3
REQUIRED_STAT_FIELDS = (
    "anon", "file", "inactive_file", "active_file", "file_dirty",
    "file_writeback", "unevictable",
)


def _bytes(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a nonnegative integer")
    return value


def conservative_working_set(sample, reclaim_factor=0.5, swap_growing=False, psi_stable=True):
    """Discount only conservative inactive file bytes under stable conditions."""
    if not isinstance(sample, dict) or not {"memory_current", "cgroup_current_bytes"} & sample.keys():
        raise ValueError("sample.cgroup_current_bytes or memory_current is required")
    currents = [_bytes(sample[key], key) for key in ("cgroup_current_bytes", "memory_current") if key in sample]
    if len(set(currents)) != 1:
        raise ValueError("conflicting cgroup_current_bytes and memory_current")
    current = currents[0]
    stats = sample.get("memory_stat")
    if not isinstance(stats, dict) or not set(REQUIRED_STAT_FIELDS) <= stats.keys():
        raise ValueError("memory_stat requires: " + ", ".join(REQUIRED_STAT_FIELDS))
    for key, value in stats.items():
        if not isinstance(key, str) or not key:
            raise ValueError("memory_stat field names must be nonempty strings")
        _bytes(value, "memory_stat." + key)
    if (type(reclaim_factor) not in {int, float} or not 0 <= reclaim_factor <= 1
            or not math.isfinite(reclaim_factor)):
        raise ValueError("reclaim_factor must be finite and within [0, 1]")
    if type(swap_growing) is not bool or type(psi_stable) is not bool:
        raise ValueError("swap_growing and psi_stable must be booleans")
    reclaimable = max(0, min(stats["file"], stats["inactive_file"])
                      - stats["file_dirty"] - stats["file_writeback"] - stats["unevictable"])
    if reclaimable > current:
        raise ValueError("reclaimable_file exceeds memory_current; inconsistent statistics")
    reasons = []
    if swap_growing:
        reasons.append("swap_growing")
    if not psi_stable:
        reasons.append("psi_not_stable")
    factor = 0 if reasons else reclaim_factor
    numerator, denominator = factor.as_integer_ratio()
    effective = reclaimable * numerator // denominator
    return {
        "memory_current": current,
        "memory_stat": dict(stats),
        "reclaim_factor": reclaim_factor,
        "applied_reclaim_factor": factor,
        "reclaimable_file": reclaimable,
        "effective_reclaimable": effective,
        "effective_working_set": current - effective,
        "effective_reclaimable_bytes": effective,
        "effective_working_set_bytes": current - effective,
        "swap_growing": swap_growing,
        "psi_stable": psi_stable,
        "admission": "wait" if reasons else "allow",
        "reasons": reasons,
        "experimental": True,
        "reclaim_guaranteed": False,
    }


def projected_admission(sample, candidate_budget, reserved_running, limit,
                        global_margin=2 * GIB, reclaim_factor=0.5,
                        swap_growing=False, psi_stable=True):
    """Project integer-byte budgets including the caller's remaining peak reserve.

    reserved_running must retain future sampler/decode peaks, not just current
    usage. Equality with limit passes this gate. raw_projected is evidence, not
    a replacement for the caller's independent raw-memory hard ceiling.
    """
    values = {"candidate_budget": candidate_budget, "reserved_running": reserved_running,
              "limit": limit, "global_margin": global_margin}
    for name, value in values.items():
        _bytes(value, name)
    if limit == 0:
        raise ValueError("limit must be positive")
    result = conservative_working_set(sample, reclaim_factor, swap_growing, psi_stable)
    additional = candidate_budget + reserved_running + global_margin
    projected = result["effective_working_set"] + additional
    raw_projected = result["memory_current"] + additional
    reasons = list(result["reasons"])
    if projected > limit:
        reasons.append("projected_working_set_exceeds_limit")
    result.update(values)
    result.update(projected=projected, raw_projected=raw_projected,
                  projected_within_limit=projected <= limit,
                  admission="wait" if reasons else "allow", reasons=reasons)
    return result
