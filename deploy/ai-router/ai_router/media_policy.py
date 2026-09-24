"""Account-level media admission, shared by every API key of an account."""
from __future__ import annotations


LIMIT_FIELDS = {
    "image_daily_requests": 1_000_000,
    "video_daily_requests": 1_000_000,
    "image_max_active": 1000,
    "video_max_active": 1000,
    "video_max_seconds": 3600,
    "paid_images_daily": 1_000_000,
}


def validate_media_limits(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {*LIMIT_FIELDS, "allow_cloud"}:
        raise ValueError("invalid media limits")
    result = {}
    for name, item in value.items():
        if name == "allow_cloud":
            if type(item) is not bool:
                raise ValueError("allow_cloud must be a boolean")
        elif type(item) is not int or not 0 <= item <= LIMIT_FIELDS[name]:
            raise ValueError("invalid " + name)
        result[name] = item
    return result


def effective_media_policy(policy) -> dict:
    limits = validate_media_limits(getattr(policy, "media_limits", {}))
    if getattr(policy, "local_only", False):
        limits["allow_cloud"] = False
    return limits
