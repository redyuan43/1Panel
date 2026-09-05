from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from .util import load_json


def collect_reports(root: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(root.glob("*/report.json")):
        value = load_json(path)
        value["_path"] = str(path)
        reports.append(value)
    return reports


def render_markdown(reports: list[dict[str, Any]]) -> str:
    lines = [
        "# NX Prefix Cache Validation Report",
        "",
        "| Mode | Transport | Cases | Cold TTFT | Warm TTFT | Reduction | Prefix reuse |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        summary = report.get("summary") or {}
        lines.append(
            "| {mode} | {transport} | {cases} | {cold} | {warm} | {reduction} | {reuse} |".format(
                mode=report.get("mode", ""),
                transport=report.get("transport", ""),
                cases=report.get("case_count", 0),
                cold=_seconds(summary.get("cold_ttft_median_seconds")),
                warm=_seconds(summary.get("warm_ttft_median_seconds")),
                reduction=_percent(summary.get("ttft_reduction_ratio")),
                reuse=_percent(summary.get("prefix_reuse_ratio_median")),
            )
        )
    all_records = [
        record
        for report in reports
        for record in report.get("records", [])
    ]
    router_overhead = _router_overhead(all_records)
    lines.extend(
        [
            "",
            "## Acceptance",
            "",
            f"- Reports: {len(reports)}",
            f"- Records: {len(all_records)}",
            f"- Cache pass records: {sum(1 for item in all_records if item.get('cache_passed'))}",
            f"- Estimated Router TTFT overhead: {_seconds(router_overhead)}",
            "",
            "A run passes the primary performance gate when stable-prefix reuse is at least 95% "
            "and median warm TTFT is at least 70% lower than cold TTFT.",
            "",
        ]
    )
    return "\n".join(lines)


def _router_overhead(records: list[dict[str, Any]]) -> float | None:
    direct: dict[tuple[str, str], list[float]] = {}
    router: dict[tuple[str, str], list[float]] = {}
    for item in records:
        key = (str(item.get("prompt_sha256")), str(item.get("suffix_sha256")))
        target = router if item.get("transport") == "router" else direct
        target.setdefault(key, []).append(float(item["first_token_seconds"]))
    deltas = []
    for key in direct.keys() & router.keys():
        deltas.append(statistics.median(router[key]) - statistics.median(direct[key]))
    return statistics.median(deltas) if deltas else None


def _seconds(value: Any) -> str:
    return "-" if value is None else f"{float(value):.3f}s"


def _percent(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"
