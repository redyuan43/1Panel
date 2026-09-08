"""Strict per-request usage evidence; no prompt content is persisted here."""
import json
import math
import re


def token_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 2**53 - 1 or (isinstance(value, float) and (not math.isfinite(value) or int(value) != value)):
        return None
    return int(value)


def usage_dict(payload=None, usage=None):
    if isinstance(usage, dict):
        return usage
    if payload:
        try:
            value = json.loads(payload)
            if isinstance(value, dict):
                current = value.get("usage")
                if not isinstance(current, dict) and isinstance(value.get("response"), dict):
                    current = value["response"].get("usage")
                if isinstance(current, dict):
                    return current
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
    return {}


def usage_measurement(payload=None, usage=None):
    value = usage_dict(payload, usage)
    inputs = [value[k] for k in ("prompt_tokens", "input_tokens") if k in value]
    cached = [value[k] for k in ("prompt_cache_hit_tokens", "cache_read_input_tokens") if k in value]
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = value.get(key)
        if isinstance(details, dict) and "cached_tokens" in details:
            cached.append(details["cached_tokens"])
    def unique(values):
        parsed = [token_count(v) for v in values]
        return parsed[0] if parsed and None not in parsed and len(set(parsed)) == 1 else None
    total, reuse = unique(inputs), unique(cached)
    invalid = bool((inputs and total is None) or (cached and reuse is None)
                   or (total is not None and reuse is not None and reuse > total))
    if invalid:
        return {"state": "invalid", "input_tokens": total, "cached_tokens": None}
    return {"state": "complete" if total is not None and reuse is not None else "missing",
            "input_tokens": total, "cached_tokens": reuse}


class UsageOnlyFilter:
    """Drop internally requested Chat usage-only SSE frames, preserving other bytes."""
    def __init__(self):
        self.buffer = b""

    @staticmethod
    def keep(frame):
        data = b"\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:"))
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return True
        return not (isinstance(value, dict) and value.get("choices") == []
                    and isinstance(value.get("usage"), dict) and not value.get("error"))

    def feed(self, chunk):
        self.buffer += chunk
        output = []
        while match := re.search(br"\r?\n\r?\n", self.buffer):
            frame, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
            if self.keep(frame):
                output.append(frame)
        # A giant malformed/extension frame must not accumulate without bound.
        if len(self.buffer) > 4 * 1024**2:
            output.append(self.buffer)
            self.buffer = b""
        return output

    def finish(self):
        remaining, self.buffer = self.buffer, b""
        return [remaining] if remaining and self.keep(remaining) else []
