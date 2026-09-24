"""Versioned image routing policy; independent of video and account grants."""
from __future__ import annotations

import copy
import hashlib
import json

DEFAULTS = {
    "enabled": True,
    "local_resources": ["nx5-image"],
    "default_route": "local_first",
    "allow_cloud": True,
    "when_busy": "cloud",
    "when_unavailable": "queue",
    "queue_limit": 10,
    "queue_timeout": 1800,
    "cloud_provider": "codex",
    "paid_fallback": False,
    "daily_paid_images": 20,
}


def validate_image_generation(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError("Invalid image_generation fields")
    result = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(value)}
    for key in ("enabled", "allow_cloud", "paid_fallback"):
        if type(result[key]) is not bool:
            raise ValueError(f"image_generation.{key} must be boolean")
    choices = {"default_route": {"local_first", "local_only", "cloud_only"},
               "when_busy": {"queue", "cloud", "error"},
               "when_unavailable": {"queue", "cloud", "error"},
               "cloud_provider": {"codex"}}
    for key, allowed in choices.items():
        if not isinstance(result[key], str) or result[key] not in allowed:
            raise ValueError(f"Invalid image_generation.{key}")
    for key, minimum, maximum in (("queue_limit", 1, 100), ("queue_timeout", 1, 86400),
                                  ("daily_paid_images", 0, 10000)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f"Invalid image_generation.{key}")
    resources = result["local_resources"]
    if (not isinstance(resources, list) or len(resources) > 100
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in resources)
            or len(set(resources)) != len(resources)):
        raise ValueError("Invalid image_generation.local_resources")
    if result["default_route"] == "cloud_only" and not result["allow_cloud"]:
        raise ValueError("Cloud-only image routing requires cloud permission")
    if result["paid_fallback"] and not result["allow_cloud"]:
        raise ValueError("Paid image fallback requires cloud permission")
    return result


def snapshot(value, revision=None):
    policy = validate_image_generation(value)
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"policy": policy, "revision": revision, "fingerprint": digest}


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {"policy", "revision", "fingerprint"}:
        raise ValueError("Invalid image policy snapshot")
    revision = value["revision"]
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError("Invalid image policy revision")
    expected = snapshot(value["policy"], revision)
    if value["fingerprint"] != expected["fingerprint"]:
        raise ValueError("Invalid image policy fingerprint")
    return expected


def cloud_allowed(job):
    policy = job["image_generation"]["policy"]
    return policy["allow_cloud"] and job.get("media_policy", {}).get("allow_cloud", True)


def route(job):
    body = job["request"]
    if body["model"] == "qwen-image-2.1" or body.get("execution") == "local":
        return "local_only"
    if body["model"] == "qwen-image-3.0-pro" or body.get("execution") == "cloud":
        return "cloud_only"
    return job["image_generation"]["policy"]["default_route"]


async def trusted_snapshot(runtime):
    manager = getattr(runtime, "policy_config", None)
    if manager is not None:
        active = (await manager.snapshot())["active"]
        return snapshot(active.get("settings", {}).get("image_generation", {}), active["revision"])
    settings = getattr(runtime, "settings", None)
    return snapshot(settings.section("image_generation")) if settings else None
