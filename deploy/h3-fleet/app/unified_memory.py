"""Explicit GB10 shared-memory admission; never manufacture discrete VRAM."""
GIB = 1024 ** 3
EDGE_UUID = "GPU-95675338-26b9-4ddf-1cc9-e449db82e19e"


def validate_policy(backend):
    policy = backend.get("unified_memory")
    if policy is None:
        return
    if (policy != {"kind": "gb10", "minimum_available_bytes": 80 * GIB,
                  "host_budget_bytes": 64 * GIB, "external_gpu_processes": "deny"}
            or backend.get("gpu_uuid") != EDGE_UUID):
        raise ValueError("unsupported_unified_memory_policy")


def admission_reasons(policy, sample, candidate, active):
    if policy is None:
        return None
    validate_policy({"unified_memory": policy, "gpu_uuid": candidate.get("gpu_uuid")})
    reasons = []
    if (candidate.get("gpu_uuid") != EDGE_UUID
            or sample.get("gpu_names", {}).get(EDGE_UUID) != "NVIDIA GB10"):
        reasons.append("unified_memory_device_not_verified")
    if sample.get("memory_available_bytes", 0) < policy["minimum_available_bytes"]:
        reasons.append("unified_memory_requires_80GiB_available")
    if candidate.get("candidate_budget_bytes", 0) < policy["host_budget_bytes"]:
        reasons.append("unified_memory_host_budget_below_64GiB")
    if active:
        reasons.append("unified_memory_single_task_only")
    owner = str(candidate.get("worker_pid"))
    if any(gpu == EDGE_UUID and pid != owner for pid, gpu in sample.get("gpu_process_identities", [])):
        reasons.append("external_gpu_workload_requires_handoff")
    if "gpu_process_identities" not in sample:
        reasons.append("gpu_process_inventory_unavailable")
    return reasons
