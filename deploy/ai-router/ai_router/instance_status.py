"""Read-only classification of persistent Router instance records."""
import math

INSTANCE_HEARTBEAT_SECONDS = 10
INSTANCE_STALE_SECONDS = 60


def classify_instances(records, fingerprint, *, now):
    result = []
    for record in records:
        item = dict(record)
        updated = item.get("updated_at")
        fresh = (isinstance(updated, (int, float)) and not isinstance(updated, bool)
                 and math.isfinite(updated) and 0 <= now - updated <= INSTANCE_STALE_SECONDS)
        if item.get("status") == "stopped":
            state = "stopped"
        elif not fresh:
            state = "stale"
        elif item.get("status") in {"running", "draining"}:
            state = "draining" if item.get("draining") or item["status"] == "draining" else "running"
        else:
            state = "unknown"
        eligible = state in {"running", "draining"}
        reported = item.get("registry_fingerprint")
        comparison = ("not_compared" if not eligible else "unknown" if not reported or not fingerprint
                      else "match" if reported == fingerprint else "mismatch")
        item.update(liveness=state, registry_comparison=comparison)
        result.append(item)
    return result
