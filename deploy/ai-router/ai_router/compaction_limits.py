"""Validated per-job limits; settings may tighten the existing safety ceilings."""

CEILINGS = {"max_seconds": 600, "max_calls": 32,
            "max_input_tokens": 1_000_000, "max_output_tokens": 64_000}


def parse_limits(value):
    if not isinstance(value, dict) or set(value) - set(CEILINGS):
        raise ValueError("compaction background limits must contain only supported budget fields")
    result = {**CEILINGS, **value}
    for key, ceiling in CEILINGS.items():
        minimum = 8192 if key == "max_output_tokens" else 1
        if type(result[key]) is not int or not minimum <= result[key] <= ceiling:
            raise ValueError(f"compaction background {key} must be an integer between {minimum} and {ceiling}")
    return result


def job_limits(job):
    return parse_limits(job["parameters"].get("limits", {}))
