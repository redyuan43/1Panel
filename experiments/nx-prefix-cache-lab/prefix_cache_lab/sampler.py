from __future__ import annotations

import random
import math
from typing import Any

from .config import SamplingConfig
from .util import digest_json


RANDOM_VALIDATION_BANDS = (
    ("small", 2000, 19999, 2),
    ("medium", 20000, 39999, 3),
    ("large", 40000, 49999, 2),
    ("near-limit", 50000, 56500, 1),
)


def estimated_tokens(item: dict[str, Any]) -> int:
    reported = int(item.get("total_tokens") or 0)
    from_chars = int(item.get("generation_input_chars") or 0) // 3
    return max(reported, from_chars)


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in sorted(items, key=lambda value: str(value.get("relative_path", ""))):
        key = str(item.get("generation_input_sha256") or digest_json(item))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def sample_catalog(
    items: list[dict[str, Any]],
    settings: SamplingConfig,
    *,
    seed: int,
) -> dict[str, Any]:
    eligible = [
        {**item, "estimated_tokens": estimated_tokens(item)}
        for item in _unique(items)
        if item.get("status") in {None, "", "success", "ok", "completed"}
        and int(item.get("generation_input_chars") or 0) > 0
    ]
    long_items = [
        item
        for item in eligible
        if settings.long_min_tokens
        <= item["estimated_tokens"]
        <= settings.long_max_tokens
    ]
    control_items = [
        item
        for item in eligible
        if settings.control_min_tokens
        <= item["estimated_tokens"]
        <= settings.control_max_tokens
    ]
    rng = random.Random(seed)
    rng.shuffle(long_items)
    rng.shuffle(control_items)
    selected = long_items[: settings.long_count]
    selected_hashes = {
        str(item.get("generation_input_sha256")) for item in selected
    }
    selected.extend(
        item
        for item in control_items
        if str(item.get("generation_input_sha256")) not in selected_hashes
    )
    selected = selected[: settings.count]
    if len(selected) < settings.count:
        already = {
            str(item.get("generation_input_sha256")) for item in selected
        }
        remainder = [
            item
            for item in eligible
            if str(item.get("generation_input_sha256")) not in already
        ]
        rng.shuffle(remainder)
        selected.extend(remainder[: settings.count - len(selected)])
    if not selected:
        raise ValueError("catalog has no eligible WorkBuddy generation traces")
    return {
        "version": 1,
        "seed": seed,
        "requested_count": settings.count,
        "selected_count": len(selected),
        "long_candidate_count": len(long_items),
        "control_candidate_count": len(control_items),
        "items": selected,
    }


def sample_candidates(
    items: list[dict[str, Any]],
    *,
    seed: int,
    count: int,
    min_input_chars: int,
) -> dict[str, Any]:
    eligible = [
        item
        for item in _unique(items)
        if item.get("status") in {None, "", "success", "ok", "completed"}
        and int(item.get("generation_input_chars") or 0) >= min_input_chars
    ]
    rng = random.Random(seed)
    rng.shuffle(eligible)
    selected = eligible[:count]
    if not selected:
        raise ValueError("catalog has no candidate traces at the requested size")
    return {
        "version": 1,
        "seed": seed,
        "selection_kind": "candidate-pool",
        "minimum_input_chars": min_input_chars,
        "candidate_count": len(eligible),
        "selected_count": len(selected),
        "items": selected,
    }


def sample_quantile_candidates(
    items: list[dict[str, Any]],
    *,
    seed: int,
    count: int = 96,
    quantiles: int = 8,
) -> dict[str, Any]:
    if count <= 0 or quantiles <= 0:
        raise ValueError("count and quantiles must be positive")
    eligible = [
        item
        for item in _unique(items)
        if item.get("status") in {None, "", "success", "ok", "completed"}
        and int(item.get("generation_input_chars") or 0) > 0
    ]
    if not eligible:
        raise ValueError("catalog has no eligible WorkBuddy generation traces")
    eligible.sort(key=lambda item: int(item.get("generation_input_chars") or 0))
    rng = random.Random(seed)
    per_quantile = max(1, math.ceil(count / quantiles))
    selected = []
    quantile_counts = []
    for index in range(quantiles):
        start = len(eligible) * index // quantiles
        end = len(eligible) * (index + 1) // quantiles
        bucket = eligible[start:end]
        rng.shuffle(bucket)
        chosen = bucket[:per_quantile]
        selected.extend({**item, "source_quantile": index + 1} for item in chosen)
        quantile_counts.append(
            {
                "quantile": index + 1,
                "candidate_count": len(bucket),
                "selected_count": len(chosen),
            }
        )
    selected = selected[:count]
    return {
        "version": 1,
        "seed": seed,
        "selection_kind": "size-quantile-random-candidates",
        "requested_count": count,
        "selected_count": len(selected),
        "eligible_count": len(eligible),
        "quantiles": quantile_counts,
        "items": selected,
    }


def select_random_validation_cases(
    profile: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    items = [
        item
        for item in profile.get("items", [])
        if 0 < int(item.get("prompt_tokens") or 0) <= 56500
    ]
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    band_results = []
    used: set[str] = set()
    for name, minimum, maximum, quota in RANDOM_VALIDATION_BANDS:
        candidates = [
            item
            for item in items
            if minimum <= int(item["prompt_tokens"]) <= maximum
            and item["case_id"] not in used
        ]
        rng.shuffle(candidates)
        chosen = candidates[:quota]
        fallback = []
        if len(chosen) < quota:
            remaining = [
                item for item in items
                if item["case_id"] not in used
                and item["case_id"] not in {value["case_id"] for value in chosen}
            ]
            target = (minimum + maximum) / 2
            remaining.sort(
                key=lambda item: (
                    abs(int(item["prompt_tokens"]) - target),
                    str(item["case_id"]),
                )
            )
            fallback = remaining[: quota - len(chosen)]
            chosen.extend(fallback)
        fallback_ids = {item["case_id"] for item in fallback}
        for item in chosen:
            used.add(item["case_id"])
            selected.append(
                {
                    **item,
                    "validation_band": name,
                    "band_match": item["case_id"] not in fallback_ids,
                }
            )
        band_results.append(
            {
                "name": name,
                "minimum_tokens": minimum,
                "maximum_tokens": maximum,
                "quota": quota,
                "candidate_count": len(candidates),
                "selected_count": len(chosen),
                "fallback_case_ids": [item["case_id"] for item in fallback],
            }
        )
    if len(selected) != sum(item[3] for item in RANDOM_VALIDATION_BANDS):
        raise ValueError("profile does not contain enough safe unique cases")
    return {
        "version": 1,
        "seed": seed,
        "selection_kind": "token-profiled-random-validation",
        "safe_prompt_tokens": 56500,
        "selected_count": len(selected),
        "bands": band_results,
        "cases": [
            {
                "case_id": item["case_id"],
                "path": item["path"],
                "prompt_tokens": item["prompt_tokens"],
                "prompt_sha256": item["prompt_sha256"],
                "validation_band": item["validation_band"],
                "band_match": item["band_match"],
            }
            for item in selected
        ],
    }


def select_profiled_cases(
    profile: dict[str, Any],
    *,
    seed: int,
    count: int,
    long_count: int,
    long_min_tokens: int,
    long_max_tokens: int,
    control_min_tokens: int,
    control_max_tokens: int,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    items = list(profile.get("items", []))
    long_items = [
        item
        for item in items
        if long_min_tokens <= int(item["prompt_tokens"]) <= long_max_tokens
    ]
    control_items = [
        item
        for item in items
        if control_min_tokens <= int(item["prompt_tokens"]) <= control_max_tokens
    ]
    if len(long_items) < long_count:
        raise ValueError(
            f"profile has only {len(long_items)} long cases; requires {long_count}"
        )
    if case_ids:
        by_id = {item["case_id"]: item for item in items}
        missing = [case_id for case_id in case_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"profile is missing selected cases: {missing}")
        selected = [by_id[case_id] for case_id in case_ids]
        if len(selected) != count or len(set(case_ids)) != count:
            raise ValueError(f"explicit selection must contain {count} unique cases")
        selected_long = sum(
            long_min_tokens <= int(item["prompt_tokens"]) <= long_max_tokens
            for item in selected
        )
        selected_control = sum(
            control_min_tokens
            <= int(item["prompt_tokens"])
            <= control_max_tokens
            for item in selected
        )
        if selected_long != long_count or selected_control != count - long_count:
            raise ValueError(
                "explicit selection does not match the required long/control split"
            )
    else:
        rng = random.Random(seed)
        rng.shuffle(long_items)
        rng.shuffle(control_items)
        selected = long_items[:long_count]
        selected_ids = {item["case_id"] for item in selected}
        selected.extend(
            item for item in control_items if item["case_id"] not in selected_ids
        )
        selected = selected[:count]
        if len(selected) < count:
            already = {value["case_id"] for value in selected}
            remainder = [
                item for item in items if item["case_id"] not in already
            ]
            rng.shuffle(remainder)
            selected.extend(remainder[: count - len(selected)])
    return {
        "version": 1,
        "seed": seed,
        "selection_kind": "token-profiled-final",
        "long_candidate_count": len(long_items),
        "control_candidate_count": len(control_items),
        "selected_count": len(selected),
        "cases": [
            {
                "case_id": item["case_id"],
                "path": item["path"],
                "prompt_tokens": item["prompt_tokens"],
                "prompt_sha256": item["prompt_sha256"],
            }
            for item in selected
        ],
    }
