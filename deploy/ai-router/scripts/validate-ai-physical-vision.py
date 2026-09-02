#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_router.media import normalize_ai_images


DEFAULT_HEALTH_URL = "http://127.0.0.1:18103/health"
DEFAULT_MODEL = "huihui/Qwen3.8-27B-Q4-DFlash2"


@dataclass(frozen=True)
class VisionCase:
    id: str
    prompt: str
    expected: str
    image_paths: tuple[Path, ...]
    required: bool = True


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output).resolve()
    fixture_dir = output_dir / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    cases = build_cases(
        fixture_dir,
        workbuddy_image=(
            Path(args.workbuddy_image).resolve()
            if args.workbuddy_image
            else None
        ),
        quick=args.quick,
    )
    started = time.monotonic()
    with httpx.Client(
        timeout=httpx.Timeout(args.timeout, connect=5.0),
        trust_env=False,
    ) as client:
        health = client.get(args.health_url)
        health.raise_for_status()
        health_payload = health.json()
    workers = [
        item
        for item in health_payload.get("workers", [])
        if (
            isinstance(item, dict)
            and item.get("ready")
            and (
                not args.worker
                or str(item.get("worker_id")) in args.worker
            )
        )
    ]
    workers.sort(
        key=lambda item: (
            int(item.get("port", 0)),
            str(item.get("worker_id", "")),
        )
    )
    if not workers:
        raise RuntimeError("no ready AI physical workers matched")

    parallel_workers = min(max(1, args.parallel_workers), len(workers))
    print(
        f"[vision] validating {len(workers)} physical workers with "
        f"parallel_workers={parallel_workers}",
        flush=True,
    )
    results_by_id: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=parallel_workers,
        thread_name_prefix="ai-vision",
    ) as executor:
        pending = {
            executor.submit(
                validate_worker_isolated,
                worker,
                cases,
                model=args.model,
                timeout=args.timeout,
                max_dimension=args.max_dimension,
                max_source_pixels=args.max_source_pixels,
            ): str(worker["worker_id"])
            for worker in workers
        }
        for future in concurrent.futures.as_completed(pending):
            worker_id = pending[future]
            try:
                results_by_id[worker_id] = future.result()
            except Exception as exc:
                results_by_id[worker_id] = {
                    "worker_id": worker_id,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "cases": [],
                    "cache_sequence": [],
                }
                print(
                    f"[vision] {worker_id}: {type(exc).__name__}: {exc}",
                    flush=True,
                )

    results = [
        results_by_id[str(worker["worker_id"])]
        for worker in workers
    ]
    payload = {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(),
        "health_url": args.health_url,
        "model": args.model,
        "quick": args.quick,
        "parallel_workers": parallel_workers,
        "wall_seconds": round(time.monotonic() - started, 3),
        "runtime_fingerprint": health_payload.get(
            "runtime_fingerprint"
        ),
        "cases": [
            {
                "id": case.id,
                "expected": case.expected,
                "required": case.required,
                "images": [
                    image_metadata(path)
                    for path in case.image_paths
                ],
            }
            for case in cases
        ],
        "workers": results,
        "passed_workers": sum(
            item["passed"] for item in results
        ),
        "total_workers": len(results),
    }
    output_path = output_dir / "vision-results.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[vision] {payload['passed_workers']}/"
        f"{payload['total_workers']} workers passed in "
        f"{payload['wall_seconds']:.2f}s; result={output_path}",
        flush=True,
    )
    return 0 if payload["passed_workers"] == len(results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate AI workers concurrently across GPUs and serially "
            "within each worker."
        )
    )
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workbuddy-image")
    parser.add_argument("--worker", action="append", default=[])
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=6,
        help="Number of physical workers to validate concurrently.",
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-dimension", type=int, default=1024)
    parser.add_argument("--max-source-pixels", type=int, default=40_000_000)
    return parser.parse_args()


def build_cases(
    fixture_dir: Path,
    *,
    workbuddy_image: Path | None,
    quick: bool,
) -> tuple[VisionCase, ...]:
    square = fixture_dir / "red-white-square-128.png"
    create_red_white_square(square)
    screenshot = (
        workbuddy_image
        if workbuddy_image and workbuddy_image.is_file()
        else fixture_dir / "workbuddy-like-screen.png"
    )
    if screenshot.parent == fixture_dir:
        create_workbuddy_like_screen(screenshot)
    landscape = fixture_dir / "landscape-1920x1080.png"
    create_split_image(
        landscape,
        (1920, 1080),
        left=(0, 80, 255),
        right=(255, 220, 0),
    )
    portrait = fixture_dir / "bands-2560x1440.png"
    create_banded_image(
        portrait,
        (2560, 1440),
        top=(0, 190, 90),
        bottom=(220, 0, 180),
    )
    red_circle = fixture_dir / "red-circle.png"
    green_triangle = fixture_dir / "green-triangle.png"
    create_shape(red_circle, "circle", (235, 20, 20))
    create_shape(green_triangle, "triangle", (20, 190, 80))
    cases = [
        VisionCase(
            id="red-white-square",
            prompt=(
                "只回复 RED_BACKGROUND_WHITE_SQUARE，"
                "如果图片不是红色背景中央白色方块则回复 OTHER。"
            ),
            expected="RED_BACKGROUND_WHITE_SQUARE",
            image_paths=(square,),
        ),
        VisionCase(
            id="workbuddy-screen",
            prompt=(
                "只判断图片中是否有一个明显的红色方块。"
                "有则只回复 RED_SQUARE，否则只回复 OTHER。"
            ),
            expected="RED_SQUARE",
            image_paths=(screenshot,),
        ),
        VisionCase(
            id="large-landscape-normalized",
            prompt=(
                "只回复 LEFT_BLUE_RIGHT_YELLOW，"
                "除非图片不是左蓝右黄。"
            ),
            expected="LEFT_BLUE_RIGHT_YELLOW",
            image_paths=(landscape,),
        ),
    ]
    if quick:
        return tuple(cases)
    cases.extend(
        [
            VisionCase(
                id="large-bands-normalized",
                prompt=(
                    "只回复 TOP_GREEN_BOTTOM_MAGENTA，"
                    "除非图片不是上绿下紫红。"
                ),
                expected="TOP_GREEN_BOTTOM_MAGENTA",
                image_paths=(portrait,),
            ),
            VisionCase(
                id="two-image-order",
                prompt=(
                    "第一张和第二张图片分别是什么？"
                    "如果依次为红色圆形和绿色三角形，"
                    "只回复 FIRST_RED_CIRCLE_SECOND_GREEN_TRIANGLE，"
                    "否则只回复 OTHER。"
                ),
                expected="FIRST_RED_CIRCLE_SECOND_GREEN_TRIANGLE",
                image_paths=(red_circle, green_triangle),
                required=False,
            ),
        ]
    )
    return tuple(cases)


def validate_worker_isolated(
    worker: dict[str, Any],
    cases: tuple[VisionCase, ...],
    *,
    model: str,
    timeout: float,
    max_dimension: int,
    max_source_pixels: int,
) -> dict[str, Any]:
    print(
        f"[vision] testing {worker['worker_id']} "
        f"on port {worker['port']}",
        flush=True,
    )
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=5.0),
        trust_env=False,
    ) as client:
        return validate_worker(
            client,
            worker,
            cases,
            model=model,
            max_dimension=max_dimension,
            max_source_pixels=max_source_pixels,
        )


def validate_worker(
    client: httpx.Client,
    worker: dict[str, Any],
    cases: tuple[VisionCase, ...],
    *,
    model: str,
    max_dimension: int,
    max_source_pixels: int,
) -> dict[str, Any]:
    api_base = str(
        worker.get("api_base")
        or f"http://127.0.0.1:{int(worker['port'])}/v1"
    ).rstrip("/")
    case_results = [
        run_case(
            client,
            api_base,
            case,
            model=model,
            max_dimension=max_dimension,
            max_source_pixels=max_source_pixels,
        )
        for case in cases
    ]
    cache_results = []
    if len(cases) > 3:
        cache_results = run_cache_sequence(
            client,
            api_base,
            model=model,
            first=cases[0],
            second=cases[3],
            max_dimension=max_dimension,
            max_source_pixels=max_source_pixels,
        )
    required_results = [
        item
        for case, item in zip(cases, case_results, strict=True)
        if case.required
    ]
    passed = all(item["passed"] for item in required_results) and all(
        item["passed"] for item in cache_results
    )
    multi_image_supported = next(
        (
            item["passed"]
            for item in case_results
            if item["case_id"] == "two-image-order"
        ),
        None,
    )
    return {
        "worker_id": worker.get("worker_id"),
        "port": worker.get("port"),
        "tier": worker.get("tier"),
        "gpu_uuids": worker.get("gpu_uuids", []),
        "names": worker.get("names", []),
        "context_size": worker.get("context_size"),
        "safe_context_tokens": worker.get("safe_context_tokens"),
        "cache_type_k": worker.get("cache_type_k"),
        "cache_type_v": worker.get("cache_type_v"),
        "declared_max_images": 1,
        "multi_image_supported": multi_image_supported,
        "passed": passed,
        "cases": case_results,
        "cache_sequence": cache_results,
    }


def run_case(
    client: httpx.Client,
    api_base: str,
    case: VisionCase,
    *,
    model: str,
    max_dimension: int,
    max_source_pixels: int,
) -> dict[str, Any]:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": case.prompt},
                *[
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url(path)},
                    }
                    for path in case.image_paths
                ],
            ],
        }
    ]
    return send_request(
        client,
        api_base,
        model=model,
        messages=messages,
        case_id=case.id,
        expected=case.expected,
        max_dimension=max_dimension,
        max_source_pixels=max_source_pixels,
    )


def run_cache_sequence(
    client: httpx.Client,
    api_base: str,
    *,
    model: str,
    first: VisionCase,
    second: VisionCase,
    max_dimension: int,
    max_source_pixels: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    results = []
    for turn, case in enumerate((first, second, first), start=1):
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "只判断本轮最后提供的图片。"
                            + case.prompt
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url(case.image_paths[0])
                        },
                    },
                ],
            }
        )
        result = send_request(
            client,
            api_base,
            model=model,
            messages=messages,
            case_id=f"cache-a-b-a-{turn}",
            expected=case.expected,
            max_dimension=max_dimension,
            max_source_pixels=max_source_pixels,
        )
        results.append(result)
        messages.append(
            {
                "role": "assistant",
                "content": result.get("content", ""),
            }
        )
    return results


def send_request(
    client: httpx.Client,
    api_base: str,
    *,
    model: str,
    messages: list[dict[str, Any]],
    case_id: str,
    expected: str,
    max_dimension: int,
    max_source_pixels: int,
) -> dict[str, Any]:
    body, resized = normalize_ai_images(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 64,
            "cache_prompt": True,
            "stream": False,
        },
        max_dimension=max_dimension,
        max_source_pixels=max_source_pixels,
    )
    started = time.monotonic()
    try:
        response = client.post(
            f"{api_base}/chat/completions",
            headers={"X-Request-ID": f"vision-{case_id}"},
            json=body,
        )
        elapsed = time.monotonic() - started
        payload = response.json()
        content = ""
        if response.is_success:
            content = str(
                payload.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            ).strip()
        passed = response.is_success and expected in content
        print(
            f"[vision] {case_id}: HTTP {response.status_code}, "
            f"{elapsed:.2f}s, passed={passed}",
            flush=True,
        )
        return {
            "case_id": case_id,
            "status_code": response.status_code,
            "elapsed_seconds": round(elapsed, 3),
            "resized_images": resized,
            "expected": expected,
            "content": content[:1000],
            "passed": passed,
            "usage": payload.get("usage"),
            "timings": payload.get("timings"),
            "error": payload.get("error"),
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(
            f"[vision] {case_id}: {type(exc).__name__}, "
            f"{elapsed:.2f}s",
            flush=True,
        )
        return {
            "case_id": case_id,
            "status_code": None,
            "elapsed_seconds": round(elapsed, 3),
            "resized_images": resized,
            "expected": expected,
            "content": "",
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    media_type = {
        ".bmp": "image/bmp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def image_metadata(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    with Image.open(io.BytesIO(payload)) as image:
        size = list(image.size)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "size": size,
    }


def create_red_white_square(path: Path) -> None:
    image = Image.new("RGB", (128, 128), (253, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 88, 88), fill=(255, 255, 255))
    image.save(path)


def create_workbuddy_like_screen(path: Path) -> None:
    image = Image.new("RGB", (1280, 720), (245, 247, 249))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1280, 64), fill=(26, 31, 38))
    draw.rectangle((70, 150, 440, 520), fill=(255, 20, 30))
    draw.rectangle((520, 160, 1190, 215), fill=(210, 215, 220))
    draw.rectangle((520, 250, 1030, 300), fill=(220, 225, 230))
    image.save(path)


def create_split_image(
    path: Path,
    size: tuple[int, int],
    *,
    left: tuple[int, int, int],
    right: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", size, left)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (size[0] // 2, 0, size[0], size[1]),
        fill=right,
    )
    image.save(path)


def create_banded_image(
    path: Path,
    size: tuple[int, int],
    *,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (0, size[1] // 2, size[0], size[1]),
        fill=bottom,
    )
    image.save(path)


def create_shape(
    path: Path,
    shape: str,
    color: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", (512, 512), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    if shape == "circle":
        draw.ellipse((96, 96, 416, 416), fill=color)
    else:
        draw.polygon(((256, 70), (70, 430), (442, 430)), fill=color)
    image.save(path)


if __name__ == "__main__":
    raise SystemExit(main())
