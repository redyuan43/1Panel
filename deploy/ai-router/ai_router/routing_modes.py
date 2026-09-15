"""Explicit routing objectives and bounded performance observations."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import time
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

MODES = ("cost", "efficiency", "quality")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FLASH = {"general": ["cloud-deepseek-v4-flash", "zhipu-glm-5.3-flash"],
         "code": ["zhipu-glm-5.3-flash", "cloud-deepseek-v4-flash"],
         "multimodal": ["zhipu-glm-5.3-flash"]}
QUALITY = {"general": ["cloud-deepseek-v4-pro", "codex-pro-gpt-6-astra", "zhipu-glm-5.3"],
           "code": ["codex-pro-gpt-6-astra", "cloud-deepseek-v4-pro", "zhipu-glm-5.3"],
           "multimodal": ["codex-pro-gpt-6-astra"]}
SCHEDULE = {"enabled": False, "timezone": "Asia/Shanghai",
            "work_windows": [{"days": ["MO", "TU", "WE", "TH", "FR"],
                              "ranges": ["09:00-12:00", "14:00-18:00"]}],
            "work_flash_order": {}, "off_hours_flash_order": {}}
DEFAULTS = {
    "enabled": False, "mode": "efficiency", "local_only": False, "observe_only": False,
    "quality_flash_fallback": True, "allow_advisory_output_limit": True,
    "flash_order": FLASH, "quality_order": QUALITY, "schedule": SCHEDULE,
    "schedule_window": None,
    "performance": {"max_first_output_seconds": 180, "min_decode_tps": 10,
                    "slowdown_ratio": .5, "window_seconds": 1800, "max_samples": 100,
                    "min_samples": 5, "min_output_tokens": 128, "min_decode_seconds": 5,
                    "min_saving_ratio": .3, "min_saving_seconds": 15, "cooldown_seconds": 600},
}
PREFIX = "router:performance:v1:"


def settings_value(routing):
    value = copy.deepcopy(DEFAULTS)
    patch = routing.get("objectives", {})
    for key, item in patch.items():
        if key == "performance" and isinstance(item, dict):
            value[key].update(item)
        elif key == "schedule" and isinstance(item, dict):
            _merge_schedule(value[key], item)
        else:
            value[key] = copy.deepcopy(item)
    return value


def _merge_schedule(target, patch):
    for key, item in patch.items():
        if key in ("work_flash_order", "off_hours_flash_order") and isinstance(item, dict):
            target[key] = {**target.get(key, {}), **copy.deepcopy(item)}
        else:
            target[key] = copy.deepcopy(item)


def _minutes(value):
    text = str(value or "").strip()
    hours, separator, minutes = text.partition(":")
    if not separator or not hours.isdigit() or len(minutes) != 2 or not minutes.isdigit():
        raise ValueError("invalid schedule time: " + text)
    return int(hours) * 60 + int(minutes)


def _matches_window(schedule, moment):
    weekday = WEEKDAYS[moment.weekday()]
    minute = moment.hour * 60 + moment.minute
    for window in schedule.get("work_windows") or []:
        if weekday not in (window.get("days") or []):
            continue
        for span in window.get("ranges") or []:
            start, _, end = str(span).partition("-")
            if _minutes(start) <= minute < _minutes(end):
                return True
    return False


def in_work_window(schedule, moment=None):
    """True inside the work window, False outside, None when the schedule is off."""
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return None
    zone = ZoneInfo(str(schedule.get("timezone") or "Asia/Shanghai"))
    moment = moment if moment is not None else datetime.now(zone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return _matches_window(schedule, moment.astimezone(zone))


def flash_order_for_window(schedule, moment=None):
    """Flash order overrides that apply right now; empty when unconfigured or off."""
    inside = in_work_window(schedule, moment)
    if inside is None:
        return {}
    override = schedule.get("work_flash_order" if inside else "off_hours_flash_order") or {}
    return copy.deepcopy(override) if isinstance(override, dict) else {}


def resolve(routing, client_mode="inherit", client_local_only=False):
    value = settings_value(routing)
    value["source"] = "global" if client_mode == "inherit" else "account"
    if client_mode != "inherit":
        value["mode"] = client_mode
    value["local_only"] = value["local_only"] or client_local_only
    schedule = value.get("schedule") or {}
    inside = in_work_window(schedule)
    value["schedule_window"] = None if inside is None else ("work" if inside else "off_hours")
    if inside is not None:
        override = schedule.get("work_flash_order" if inside else "off_hours_flash_order") or {}
        if isinstance(override, dict) and override:
            value["flash_order"] = {**value["flash_order"], **copy.deepcopy(override)}
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("routing.objectives contains unknown fields")
    if value.get("schedule_window") is not None:
        raise ValueError("routing.objectives.schedule_window is derived and must not be set")
    full = settings_value({"objectives": value})
    if full["mode"] not in MODES:
        raise ValueError("routing.objectives.mode must be cost, efficiency or quality")
    for key in ("enabled", "local_only", "observe_only", "quality_flash_fallback", "allow_advisory_output_limit"):
        if not isinstance(full[key], bool):
            raise ValueError("routing.objectives." + key + " must be boolean")
    for key in ("flash_order", "quality_order"):
        order = full[key]
        if not isinstance(order, dict) or set(order) != set(FLASH):
            raise ValueError(key + " must define general, code and multimodal")
        for values in order.values():
            if (not isinstance(values, list) or not values or len(values) > 10
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(set(values)) != len(values)):
                raise ValueError(key + " must contain unique endpoint IDs")
    _validate_schedule(full["schedule"])
    limits = {"max_first_output_seconds": (1, 1800), "min_decode_tps": (.1, 1000),
              "slowdown_ratio": (.05, .95), "window_seconds": (60, 86400),
              "max_samples": (5, 100), "min_samples": (5, 100),
              "min_output_tokens": (16, 4096), "min_decode_seconds": (1, 120),
              "min_saving_ratio": (.01, .95), "min_saving_seconds": (1, 600),
              "cooldown_seconds": (0, 86400)}
    perf = full["performance"]
    if not isinstance(perf, dict) or set(perf) != set(limits):
        raise ValueError("invalid routing performance fields")
    for key, (low, high) in limits.items():
        v = perf[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError("invalid performance." + key)
    if int(perf["max_samples"]) != perf["max_samples"] or int(perf["min_samples"]) != perf["min_samples"] or perf["min_samples"] > perf["max_samples"]:
        raise ValueError("invalid performance sample counts")


def _validate_schedule(schedule):
    if not isinstance(schedule, dict):
        raise ValueError("routing.objectives.schedule must be an object")
    if set(schedule) - set(SCHEDULE):
        raise ValueError("routing.objectives.schedule contains unknown fields")
    if not isinstance(schedule.get("enabled"), bool):
        raise ValueError("routing.objectives.schedule.enabled must be boolean")
    zone = str(schedule.get("timezone") or "").strip()
    if not zone:
        raise ValueError("routing.objectives.schedule.timezone must not be empty")
    try:
        ZoneInfo(zone)
    except Exception as exc:
        raise ValueError("routing.objectives.schedule.timezone is not a known IANA zone") from exc
    windows = schedule.get("work_windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("routing.objectives.schedule.work_windows must be a non-empty list")
    for window in windows:
        if (not isinstance(window, dict) or set(window) != {"days", "ranges"}
                or not isinstance(window["days"], list) or not window["days"]
                or len(set(window["days"])) != len(window["days"])
                or any(day not in WEEKDAYS for day in window["days"])
                or not isinstance(window["ranges"], list) or not window["ranges"]):
            raise ValueError("invalid routing.objectives.schedule.work_windows entry")
        for span in window["ranges"]:
            start, separator, end = str(span).partition("-")
            if not separator:
                raise ValueError("schedule ranges must be HH:MM-HH:MM")
            try:
                ascending = _minutes(start) < _minutes(end) <= 1440
            except ValueError as exc:
                raise ValueError("schedule ranges must be HH:MM-HH:MM") from exc
            if not ascending:
                raise ValueError("schedule ranges must ascend within a single day")
    for key in ("work_flash_order", "off_hours_flash_order"):
        order = schedule.get(key)
        if not isinstance(order, dict):
            raise ValueError("routing.objectives.schedule." + key + " must be an object")
        for group, values in order.items():
            if group not in FLASH:
                raise ValueError("unsupported schedule group: " + str(group))
            if (not isinstance(values, list) or not values or len(values) > 10
                    or any(not isinstance(v, str) or not v.strip() for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError("routing.objectives.schedule." + key + " must contain unique endpoint IDs")


def task_group(evaluation):
    if evaluation.route_profile.startswith("multimodal"):
        return "multimodal"
    if evaluation.route_profile in {"code", "complex_code"} or evaluation.task == "code":
        return "code"
    return "general"


def advisory(endpoint, options):
    return bool(options.get("enabled") and options.get("mode") == "quality"
                and options.get("allow_advisory_output_limit")
                and endpoint.id in {e for row in options["quality_order"].values() for e in row}
                and not endpoint.capabilities.output_token_limit
                and endpoint.metadata.get("output_limit_status") == "unsupported-chatgpt-subscription")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def group_key(endpoint_id, tokens, reasoning, cache):
    bucket = max(32768, 2 ** max(0, (max(1, int(tokens)) - 1).bit_length()))
    return hashlib.sha256(json.dumps([endpoint_id, bucket, reasoning, cache]).encode()).hexdigest()


class PerformanceRouter:
    def __init__(self, store):
        self.store = store

    async def samples(self, endpoint_id, tokens, reasoning, cache, config):
        if not self.store:
            return []
        key = group_key(endpoint_id, tokens, reasoning, cache)
        row = await self.store.get_json(PREFIX + key) or {}
        cutoff = time.time() - config["window_seconds"]
        return [r for r in row.get("samples", []) if r.get("at", 0) >= cutoff]

    async def observe(self, trace, config):
        if not self.store or trace.get("status") != "succeeded":
            return
        observation = trace.get("performance_observation", {})
        if not observation:
            return
        key = group_key(trace["endpoint_id"], observation["input_tokens"],
                        observation["reasoning"], observation["cache"])
        lock = PREFIX + "lock:" + key
        owner = uuid4().hex
        if not await self.store.acquire_lock(lock, owner, 5):
            return
        try:
            rows = await self.samples(trace["endpoint_id"], observation["input_tokens"],
                                      observation["reasoning"], observation["cache"], config)
            if any(r["request_id"] == trace["request_id"] for r in rows):
                return
            rows.append({**observation, "at": time.time(), "request_id": trace["request_id"]})
            await self.store.set_json(PREFIX + key, {"samples": rows[-int(config["max_samples"]):]},
                                      ttl_seconds=int(config["window_seconds"]))
            # Conversation signal spans only this verified lineage; raw prompts never enter Redis.
            cid = trace.get("conversation_id")
            if cid:
                scope = hashlib.sha256((str(trace.get("client_id")) + ":" + cid).encode()).hexdigest()
                await self.store.set_json(PREFIX + "last:" + scope, rows[-1],
                                          ttl_seconds=int(config["window_seconds"]))
        finally:
            await self.store.release_lock(lock, owner)

    async def select(self, candidates, statuses, evaluation, conversation, options, tokens, output,
                     client_id, reasoning="unknown"):
        """Select at the next request boundary; this never cancels or retries inference."""
        group = task_group(evaluation)
        by_id = {e.id: e for e in candidates}
        flash = [by_id[e] for e in options["flash_order"][group] if e in by_id]
        quality = [by_id[e] for e in options["quality_order"][group] if e in by_id]
        local = [e for e in candidates if not e.cloud]
        previous = by_id.get(conversation.endpoint_id) if conversation else None
        available = lambda e: statuses[e.id].load_headroom > 0
        ranked_local = sorted(local, key=lambda e: (-statuses[e.id].load_headroom, e.id))
        evidence = {"mode": options["mode"], "source": options["source"], "local_only": options["local_only"]}
        if options["mode"] == "quality":
            ready = [e for e in quality if available(e)]
            if ready:
                return ready[0], "quality_preferred", evidence
            if options["quality_flash_fallback"] and flash:
                return next((e for e in flash if available(e)), flash[0]), "quality_flash_fallback", evidence
            if quality:
                return quality[0], "quality_wait", evidence
            return None, "quality_unavailable", evidence
        if options["mode"] == "cost":
            if previous and (not previous.cloud or not local):
                return previous, "cost_affinity", evidence
            if ranked_local:
                return ranked_local[0], "cost_local", evidence
            return (flash[0] if flash else None), "cost_flash_fallback", evidence

        perf = options["performance"]
        scope = hashlib.sha256((client_id + ":" + conversation.conversation_id).encode()).hexdigest() if conversation else None
        last = await self.store.get_json(PREFIX + "last:" + scope) if self.store and scope else None
        migrated = await self.store.get_json(PREFIX + "migration:" + scope) if self.store and scope else None
        last = last if previous and last and last.get("endpoint_id") == previous.id else None
        wait_value = (last.get("prefill_seconds") if last and finite(last.get("prefill_seconds"))
                      else last.get("first_output_seconds") if last else None)
        evidence["wait_source"] = "backend_prefill" if last and finite(last.get("prefill_seconds")) else "upstream_first_effective_output"
        severe = finite(wait_value) and wait_value > perf["max_first_output_seconds"]
        evidence["previous_wait_seconds"] = wait_value
        slow = False
        expected_output = min(output, 1024)
        if previous and last:
            rows = await self.samples(previous.id, tokens.get(previous.id, 0), reasoning, last.get("cache", "unknown"), perf)
            valid = [r for r in rows if finite(r.get("decode_tps"))]
            recent = valid[-3:]
            baseline_rows = valid[:-3]
            baseline = statistics.median(r["decode_tps"] for r in baseline_rows) if len(baseline_rows) >= perf["min_samples"] else None
            evidence.update(recent_decode_tps=[r["decode_tps"] for r in recent], baseline_decode_tps=baseline,
                            previous_cache=last.get("cache", "unknown"))
            if valid:
                expected_output = min(output, statistics.median(r["output_tokens"] for r in valid))
            slow = len(recent) == 3 and sum(
                r["decode_tps"] < perf["min_decode_tps"] or (baseline is not None and r["decode_tps"] < baseline * perf["slowdown_ratio"])
                for r in recent) >= 2
        estimates = {}
        sample_counts = {}
        for e in [*ranked_local, *flash]:
            cache = last.get("cache", "unknown") if previous and e.id == previous.id and last else "cold"
            rows = await self.samples(e.id, tokens.get(e.id, 0), reasoning, cache, perf)
            usable = [r for r in rows if finite(r.get("decode_tps")) and r["decode_tps"] > 0 and finite(r.get("first_output_seconds"))]
            sample_counts[e.id] = len(usable)
            if len(usable) >= perf["min_samples"]:
                estimates[e.id] = statistics.median(r["first_output_seconds"] + r.get("queue_seconds", 0) for r in usable[-3:]) + expected_output / statistics.median(r["decode_tps"] for r in usable[-3:])
        evidence.update(severe_wait=severe, sustained_slowdown=slow, estimated_seconds=estimates,
                        sample_counts=sample_counts, target_cache="cold", expected_output_tokens=expected_output)
        base = previous or next((e for e in ranked_local if available(e)), None) or (flash[0] if flash else (ranked_local[0] if ranked_local else None))
        if not base:
            return None, "efficiency_unavailable", evidence
        if previous and migrated and time.time() - migrated["at"] < perf["cooldown_seconds"]:
            return previous, "efficiency_cooldown", evidence
        alternatives = [e for e in [*ranked_local, *flash] if e.id != base.id and available(e)]
        measured = [e for e in alternatives if e.id in estimates]
        best = min(measured, key=lambda e: estimates[e.id]) if measured else None
        base_time = estimates.get(base.id)
        enough = bool(best and base_time and base_time - estimates[best.id] >= perf["min_saving_seconds"]
                      and (base_time - estimates[best.id]) / base_time >= perf["min_saving_ratio"])
        if enough and (severe or slow):
            chosen, reason = best, "efficiency_measured_gain"
        elif severe and alternatives:
            chosen = next((e for e in flash if e in alternatives), alternatives[0])
            reason = "efficiency_severe_wait"
            evidence["target_performance"] = "measured" if chosen.id in estimates else "unverified"
        else:
            chosen, reason = base, "efficiency_affinity" if previous else "efficiency_initial"
            if severe or slow:
                evidence["retained_reason"] = "no_eligible_alternative" if not alternatives else "insufficient_measured_migration_gain"
        if previous and chosen.id != previous.id and scope and self.store and not options.get("observe_only"):
            await self.store.set_json(PREFIX + "migration:" + scope, {"at": time.time(), "endpoint_id": chosen.id},
                                      ttl_seconds=max(1, int(perf["cooldown_seconds"])))
        return chosen, reason, evidence
