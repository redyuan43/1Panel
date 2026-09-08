from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


PROMPTS = [
    (
        "A cinematic wide shot of a clear mountain river flowing through dark "
        "rocks and green forest at sunrise. The camera moves slowly downstream, "
        "water motion remains physically consistent, trees keep stable shapes, "
        "and warm light reflects naturally on the surface. overall_soundscape: "
        "continuous flowing water, light wind through leaves, and distant birds; "
        "no speech and no music."
    ),
    (
        "A macro product shot of a mechanical wristwatch resting on black stone. "
        "The camera makes one slow controlled orbit while the second hand advances "
        "smoothly, engraved markings remain legible and stable, and reflections "
        "move consistently across the metal case. overall_soundscape: close, "
        "regular mechanical ticking with faint room ambience; no speech and no music."
    ),
    (
        "A locked close shot of a small campfire burning between dark stones in a "
        "quiet pine forest at blue hour. Flames move naturally without changing the "
        "shape of the stones, glowing embers rise and fade, and the background trees "
        "remain stable. overall_soundscape: clear fire crackling, occasional soft "
        "ember pops, and faint evening wind; no speech and no music."
    ),
]
SEEDS = [2026090701, 2026090702, 2026090703]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-url", required=True)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path.home() / ".config/ai-router-media/h3-key",
    )
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--width", type=int, default=864)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--length", type=int, default=124)
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--seed", type=int)
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt-file", type=Path)
    prompt_group.add_argument("--prompt-files", type=Path, nargs="+")
    parser.add_argument("--input-image", default="")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--stage", default="preview")
    parser.add_argument(
        "--profile",
        choices=("preview", "quality"),
        default="preview",
    )
    parser.add_argument(
        "--lora",
        default="t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors",
    )
    return parser.parse_args()


def router_key(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("H3 key file must be a regular 0600 file")
    value = path.read_text(encoding="utf-8").strip()
    if (
        not value
        or len(value) > 256
        or not all(character.isalnum() or character in "_-" for character in value)
    ):
        raise ValueError("H3 key file is invalid")
    return value


def select_seeds(jobs: int, seed: int | None) -> list[int]:
    if seed is not None:
        return [seed] * jobs
    return SEEDS[:jobs]


def load_prompts(
    jobs: int,
    prompt_file: Path | None,
    prompt_files: list[Path] | None,
) -> list[str]:
    if prompt_files:
        if len(prompt_files) != jobs:
            raise ValueError("--prompt-files count must match --jobs")
        return [
            path.read_text(encoding="utf-8").strip() for path in prompt_files
        ]
    if prompt_file:
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        return [prompt] * jobs
    return PROMPTS[:jobs]


def build_workflow(
    template: dict[str, Any],
    *,
    prompt: str,
    seed: int,
    index: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    workflow = copy.deepcopy(template)
    found = {
        "conditioning": False,
        "noise": False,
        "sampler": False,
        "save": False,
        "unet": False,
    }
    lora_nodes = 0
    for node in workflow.values():
        class_type = node.get("class_type")
        inputs = node.setdefault("inputs", {})
        if class_type == "UNETLoader":
            inputs["unet_name"] = "minimax_h3_fl2va_int8_convrot.safetensors"
            found["unet"] = True
        elif class_type == "LoraLoaderBypassModelOnly":
            inputs["lora_name"] = args.lora
            lora_nodes += 1
        elif class_type == "LoadImage" and args.input_image:
            inputs["image"] = args.input_image
        elif class_type in {
            "MiniMaxH3AudioConditioningT8",
            "MiniMaxH3ImageToVideo",
        }:
            inputs.update(
                prompt=prompt,
                width=args.width,
                height=args.height,
                length=args.length,
            )
            if class_type == "MiniMaxH3AudioConditioningT8":
                inputs["task_type"] = "T2VA"
            found["conditioning"] = True
        elif class_type == "RandomNoise":
            inputs["noise_seed"] = seed
            found["noise"] = True
        elif class_type == "MiniMaxH3DualClockSamplerT8":
            inputs["steps"] = args.steps
            found["sampler"] = True
        elif class_type == "BasicScheduler":
            inputs["steps"] = args.steps
            found["sampler"] = True
        elif class_type == "SaveVideo":
            inputs["filename_prefix"] = (
                f"video/h3-fleet-validation/{index + 1}-{uuid.uuid4().hex[:8]}"
            )
            found["save"] = True
    missing = [name for name, present in found.items() if not present]
    if lora_nodes and not args.lora:
        missing.append("lora")
    if missing:
        raise RuntimeError(f"template missing required nodes: {missing}")
    return workflow


def find_video(record: dict[str, Any]) -> dict[str, str] | None:
    for output in record.get("outputs", {}).values():
        for key in ("videos", "images"):
            for item in output.get(key, []):
                filename = str(item.get("filename", ""))
                if filename.lower().endswith((".mp4", ".mov", ".webm")):
                    return {
                        "filename": filename,
                        "subfolder": str(item.get("subfolder", "")),
                        "type": str(item.get("type", "output")),
                    }
    return None


def system_sample() -> dict[str, Any]:
    meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
    values = {}
    for line in meminfo.splitlines():
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        if name in {"MemAvailable", "SwapTotal", "SwapFree"}:
            values[name] = int(raw.split()[0]) * 1024
    gpu_raw = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    gpus = []
    for line in gpu_raw.splitlines():
        index, gpu_uuid, memory, utilization, temperature = [
            item.strip() for item in line.split(",")
        ]
        gpus.append(
            {
                "index": int(index),
                "uuid": gpu_uuid,
                "memory_used_mib": int(memory),
                "utilization_percent": int(utilization),
                "temperature_c": int(temperature),
            }
        )
    processes = []
    for lane in ("fast", "main", "preview"):
        unit = f"comfyui-h3@{lane}.service"
        pid = int(
            subprocess.check_output(
                ["systemctl", "show", unit, "-p", "MainPID", "--value"],
                text=True,
            ).strip()
            or 0
        )
        metric = {"lane": lane, "pid": pid, "rss_bytes": 0, "pss_bytes": 0}
        if pid:
            status_path = Path(f"/proc/{pid}/status")
            smaps_path = Path(f"/proc/{pid}/smaps_rollup")
            if status_path.exists():
                for line in status_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("VmRSS:"):
                        metric["rss_bytes"] = int(line.split()[1]) * 1024
                        break
            if smaps_path.exists():
                for line in smaps_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("Pss:"):
                        metric["pss_bytes"] = int(line.split()[1]) * 1024
                        break
        processes.append(metric)
    root = shutil.disk_usage("/")
    offload = shutil.disk_usage("/mnt/ivan-ext4-offload")
    return {
        "timestamp": time.time(),
        "memory_available_bytes": values["MemAvailable"],
        "swap_used_bytes": values["SwapTotal"] - values["SwapFree"],
        "gpus": gpus,
        "processes": processes,
        "root_available_bytes": root.free,
        "offload_available_bytes": offload.free,
    }


async def monitor(stop: asyncio.Event, path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        while not stop.is_set():
            handle.write(json.dumps(system_sample(), ensure_ascii=True) + "\n")
            handle.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass


async def wait_for_job(
    client: httpx.AsyncClient,
    fleet_url: str,
    prompt_id: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            response = await client.get(
                f"{fleet_url}/history/{prompt_id}",
                timeout=30,
            )
        except httpx.TransportError:
            await asyncio.sleep(5)
            continue
        response.raise_for_status()
        record = response.json().get(prompt_id)
        if record:
            status = record.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"job {prompt_id} failed: {status.get('messages')}")
            output = find_video(record)
            if output:
                return record, output
        await asyncio.sleep(5)
    raise TimeoutError(f"job {prompt_id} exceeded {timeout} seconds")


def ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    return json.loads(result)


def parse_fraction(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return float(value)
    return float(numerator) / float(denominator)


def validate_media(
    probe: dict[str, Any],
    *,
    width: int,
    height: int,
    length: int,
    fps: float = 24.0,
) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    if video is None:
        raise RuntimeError("output has no video stream")
    if audio is None:
        raise RuntimeError("output has no audio stream")
    actual_width = int(video.get("width", 0))
    actual_height = int(video.get("height", 0))
    if (actual_width, actual_height) != (width, height):
        raise RuntimeError(
            f"unexpected resolution {actual_width}x{actual_height}, "
            f"expected {width}x{height}"
        )
    actual_fps = parse_fraction(
        str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0")
    )
    if abs(actual_fps - fps) > 0.1:
        raise RuntimeError(f"unexpected frame rate {actual_fps}, expected {fps}")
    duration = float(
        video.get("duration")
        or probe.get("format", {}).get("duration")
        or 0
    )
    expected_duration = length / fps
    if abs(duration - expected_duration) > 0.25:
        raise RuntimeError(
            f"unexpected duration {duration:.3f}s, "
            f"expected about {expected_duration:.3f}s"
        )
    frame_count = int(video["nb_frames"]) if video.get("nb_frames") else round(
        duration * actual_fps
    )
    if abs(frame_count - length) > 1:
        raise RuntimeError(
            f"unexpected frame count {frame_count}, expected {length}"
        )
    return {
        "ok": True,
        "width": actual_width,
        "height": actual_height,
        "fps": actual_fps,
        "frame_count": frame_count,
        "duration_seconds": duration,
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": audio.get("sample_rate"),
        "audio_channels": audio.get("channels"),
    }


def summarize_metrics(path: Path) -> dict[str, Any]:
    samples = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not samples:
        raise RuntimeError("metrics file contains no samples")
    gpu_summary: dict[str, dict[str, Any]] = {}
    process_summary: dict[str, dict[str, Any]] = {}
    for sample in samples:
        for gpu in sample["gpus"]:
            summary = gpu_summary.setdefault(
                gpu["uuid"],
                {
                    "index": gpu["index"],
                    "max_memory_used_mib": 0,
                    "max_utilization_percent": 0,
                    "max_temperature_c": 0,
                },
            )
            summary["max_memory_used_mib"] = max(
                summary["max_memory_used_mib"],
                gpu["memory_used_mib"],
            )
            summary["max_utilization_percent"] = max(
                summary["max_utilization_percent"],
                gpu["utilization_percent"],
            )
            summary["max_temperature_c"] = max(
                summary["max_temperature_c"],
                gpu["temperature_c"],
            )
        for process in sample["processes"]:
            summary = process_summary.setdefault(
                process["lane"],
                {"max_rss_bytes": 0, "max_pss_bytes": 0},
            )
            summary["max_rss_bytes"] = max(
                summary["max_rss_bytes"],
                process["rss_bytes"],
            )
            summary["max_pss_bytes"] = max(
                summary["max_pss_bytes"],
                process["pss_bytes"],
            )
    return {
        "samples": len(samples),
        "min_memory_available_bytes": min(
            sample["memory_available_bytes"] for sample in samples
        ),
        "max_swap_used_bytes": max(sample["swap_used_bytes"] for sample in samples),
        "min_root_available_bytes": min(
            sample["root_available_bytes"] for sample in samples
        ),
        "min_offload_available_bytes": min(
            sample["offload_available_bytes"] for sample in samples
        ),
        "gpus": gpu_summary,
        "processes": process_summary,
    }


async def run(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": "Bearer " + router_key(args.key_file)}
    template = json.loads(args.template.read_text(encoding="utf-8"))
    prompts = load_prompts(args.jobs, args.prompt_file, args.prompt_files)
    seeds = select_seeds(args.jobs, args.seed)
    workflows = [
        build_workflow(
            template,
            prompt=prompts[index],
            seed=seeds[index],
            index=index,
            args=args,
        )
        for index in range(args.jobs)
    ]
    (args.output_dir / "workflows.json").write_text(
        json.dumps(workflows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    stop = asyncio.Event()
    monitor_task = asyncio.create_task(monitor(stop, args.output_dir / "metrics.jsonl"))
    started = time.time()
    report: dict[str, Any] = {
        "started_at": started,
        "configuration": {
            "steps": args.steps,
            "width": args.width,
            "height": args.height,
            "length": args.length,
            "jobs": args.jobs,
            "seed": args.seed,
            "prompt_file": str(args.prompt_file) if args.prompt_file else None,
            "prompt_files": (
                [str(path) for path in args.prompt_files]
                if args.prompt_files
                else None
            ),
            "input_image": args.input_image or None,
            "lora": args.lora,
            "stage": args.stage,
            "profile": args.profile,
        },
        "jobs": [],
    }
    try:
        async with httpx.AsyncClient(timeout=180, headers=headers) as client:
            submissions = await asyncio.gather(
                *[
                    client.post(
                        f"{args.fleet_url}/prompt",
                        json={
                            "prompt": workflow,
                            "extra_data": {
                                "h3": {
                                    "stage": args.stage,
                                    "profile": args.profile,
                                    "validation_index": index,
                                }
                            },
                        },
                    )
                    for index, workflow in enumerate(workflows)
                ]
            )
            submitted = []
            for index, response in enumerate(submissions):
                response.raise_for_status()
                payload = response.json()
                submitted.append(payload)
                report["jobs"].append(
                    {
                        "index": index,
                        "prompt": prompts[index],
                        "seed": seeds[index],
                        "prompt_id": payload["prompt_id"],
                        "lane": payload.get("h3_lane"),
                        "gpu_uuid": payload.get("h3_gpu_uuid"),
                    }
                )

            completed = await asyncio.gather(
                *[
                    wait_for_job(
                        client,
                        args.fleet_url,
                        payload["prompt_id"],
                        args.timeout,
                    )
                    for payload in submitted
                ]
            )
            for job, (_, output) in zip(report["jobs"], completed, strict=True):
                destination = args.output_dir / f"lane-{job['index'] + 1}.mp4"
                response = await client.get(
                    f"{args.fleet_url}/view",
                    params=output,
                    timeout=300,
                )
                response.raise_for_status()
                destination.write_bytes(response.content)
                job["output"] = output
                job["local_artifact"] = str(destination)
                job["ffprobe"] = ffprobe(destination)
                job["media_validation"] = validate_media(
                    job["ffprobe"],
                    width=args.width,
                    height=args.height,
                    length=args.length,
                )
    finally:
        stop.set()
        await monitor_task
        report["finished_at"] = time.time()
        report["elapsed_seconds"] = report["finished_at"] - started
        report["final_system"] = system_sample()
        report["metrics_summary"] = summarize_metrics(
            args.output_dir / "metrics.jsonl"
        )
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
