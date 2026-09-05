from prefix_cache_lab.config import SamplingConfig
from prefix_cache_lab.sampler import (
    sample_candidates,
    sample_catalog,
    sample_quantile_candidates,
    select_profiled_cases,
    select_random_validation_cases,
)


def _item(index: int, tokens: int) -> dict:
    return {
        "relative_path": f"{index}/trace.json",
        "status": "success",
        "total_tokens": tokens,
        "generation_input_chars": tokens * 3,
        "generation_input_sha256": f"{index:064x}",
    }


def test_sampling_is_reproducible_and_stratified() -> None:
    items = [_item(index, 45000) for index in range(10)]
    items.extend(_item(100 + index, 25000) for index in range(5))
    settings = SamplingConfig()
    first = sample_catalog(items, settings, seed=42)
    second = sample_catalog(items, settings, seed=42)
    assert first == second
    assert len(first["items"]) == 8
    assert sum(item["estimated_tokens"] >= 40000 for item in first["items"]) == 6


def test_sampling_deduplicates_generation_inputs() -> None:
    first = _item(1, 45000)
    duplicate = {**_item(2, 45000), "generation_input_sha256": first["generation_input_sha256"]}
    result = sample_catalog(
        [first, duplicate],
        SamplingConfig(count=1, long_count=1),
        seed=1,
    )
    assert result["selected_count"] == 1


def test_candidate_sampling_uses_character_floor() -> None:
    items = [_item(index, 1000) for index in range(5)]
    for index, item in enumerate(items):
        item["generation_input_chars"] = 80000 + index * 10000
    result = sample_candidates(
        items,
        seed=7,
        count=2,
        min_input_chars=100000,
    )
    assert result["selected_count"] == 2
    assert all(item["generation_input_chars"] >= 100000 for item in result["items"])


def test_profiled_selection_requires_true_long_cases() -> None:
    items = [
        {
            "case_id": f"long-{index}",
            "path": f"/tmp/long-{index}.json",
            "prompt_tokens": 45000,
            "prompt_sha256": str(index),
        }
        for index in range(6)
    ]
    items.extend(
        {
            "case_id": f"control-{index}",
            "path": f"/tmp/control-{index}.json",
            "prompt_tokens": 25000,
            "prompt_sha256": f"c{index}",
        }
        for index in range(2)
    )
    result = select_profiled_cases(
        {"items": items},
        seed=8,
        count=8,
        long_count=6,
        long_min_tokens=40000,
        long_max_tokens=55000,
        control_min_tokens=20000,
        control_max_tokens=39999,
    )
    assert result["selected_count"] == 8
    assert sum(case["prompt_tokens"] >= 40000 for case in result["cases"]) == 6


def test_profiled_selection_accepts_explicit_agent_choice() -> None:
    items = [
        {
            "case_id": f"long-{index}",
            "path": f"/tmp/long-{index}.json",
            "prompt_tokens": 45000,
            "prompt_sha256": str(index),
        }
        for index in range(6)
    ]
    items.extend(
        {
            "case_id": f"control-{index}",
            "path": f"/tmp/control-{index}.json",
            "prompt_tokens": 25000,
            "prompt_sha256": f"c{index}",
        }
        for index in range(2)
    )
    selected_ids = [item["case_id"] for item in reversed(items)]
    result = select_profiled_cases(
        {"items": items},
        seed=9,
        count=8,
        long_count=6,
        long_min_tokens=40000,
        long_max_tokens=55000,
        control_min_tokens=20000,
        control_max_tokens=39999,
        case_ids=selected_ids,
    )
    assert [item["case_id"] for item in result["cases"]] == selected_ids


def test_random_candidates_cover_size_quantiles_reproducibly() -> None:
    items = [_item(index, 1000 + index * 100) for index in range(80)]
    first = sample_quantile_candidates(items, seed=123, count=16, quantiles=4)
    second = sample_quantile_candidates(items, seed=123, count=16, quantiles=4)
    assert first == second
    assert first["selected_count"] == 16
    assert {item["source_quantile"] for item in first["items"]} == {1, 2, 3, 4}


def test_random_validation_uses_actual_token_bands() -> None:
    token_values = [3000, 12000, 21000, 28000, 39000, 42000, 49000, 52000]
    items = [
        {
            "case_id": f"case-{index}",
            "path": f"/tmp/case-{index}.json",
            "prompt_tokens": tokens,
            "prompt_sha256": str(index),
        }
        for index, tokens in enumerate(token_values)
    ]
    result = select_random_validation_cases({"items": items}, seed=321)
    assert result["selected_count"] == 8
    counts = {}
    for case in result["cases"]:
        counts[case["validation_band"]] = counts.get(case["validation_band"], 0) + 1
    assert counts == {"small": 2, "medium": 3, "large": 2, "near-limit": 1}
    assert all(not band["fallback_case_ids"] for band in result["bands"])
    assert all(case["band_match"] for case in result["cases"])
