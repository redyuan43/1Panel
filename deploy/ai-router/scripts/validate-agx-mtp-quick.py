#!/usr/bin/env python3
"""Bounded AGX MTP trials; restores the original service without changing routing."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from uuid import uuid4
import zlib

import httpx


ENDPOINT = "agx-qwen36-cerebellum-256k"
MODEL = "Cerebellum-v1-Q3_K_M.gguf"
ORIGINAL = "agx-cerebellum.service"
ARTIFACTS = "/data/agx-runtimes/cerebellum-mtp-20260904"
MERGED = ARTIFACTS + "/Cerebellum-v1-Q3_K_M-with-MTP.gguf"
DIRECT = "http://agx.taild500c8.ts.net:8080"
CONTROL = "http://127.0.0.1:4001"
LOCAL_TEST = "http://127.0.0.1:18081"
PARAMETER_PROFILES = {
    "baseline": {},
    "cache": {"--cache-ram": "4096", "--ctx-checkpoints": "8"},
    "batch": {"--cache-ram": "4096", "--ctx-checkpoints": "8", "-b": "1024", "-ub": "256"},
    "threads": {"--cache-ram": "4096", "--ctx-checkpoints": "8", "--threads": "6", "--threads-batch": "8"},
    "combined": {"--cache-ram": "4096", "--ctx-checkpoints": "8", "-b": "1024", "-ub": "256",
                 "--threads": "6", "--threads-batch": "8"},
}


def competitor_journal(since: int) -> str:
    return ssh("journalctl", "-u", "agx-hymt-translate.service",
               "--since=@" + str(since), "--no-pager", check=True)


def has_competitor_activity(journal: str) -> bool:
    return any(marker in journal for marker in (
        "processing task", "main: loading model", "load_tensors:",
        "Started AGX", "Stopping AGX", "Stopped AGX",
    ))


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def ssh(*args: str, timeout: int = 30, check: bool = True) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "agx", shlex.join(args)],
        text=True, capture_output=True, timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(f"remote command failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def replace_flag(argv: list[str], flag: str, value: str) -> None:
    argv[argv.index(flag) + 1] = value


def shutdown_status(unit: str) -> dict:
    output = ssh("systemctl", "show", unit, "-p", "Result", "-p", "ExecMainCode",
                 "-p", "ExecMainStatus", "-p", "MainPID", "-p", "ActiveState")
    status = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    status["passed"] = (
        status.get("Result") == "success" and status.get("MainPID") == "0"
        and status.get("ActiveState") == "inactive"
    )
    return status


def stop_trial(unit: str) -> None:
    # AGX CUDA cleanup can exceed 30s; allow systemd to finish its stop job.
    ssh("sudo", "-n", "systemctl", "stop", unit, timeout=120, check=False)


def text_argv(original: list[str], depth: int, *, no_mmap: bool = False) -> list[str]:
    if depth not in (0, 1, 2, 4):
        raise ValueError("unsupported trial depth")
    argv = list(original)
    if "--mmproj" in argv:
        index = argv.index("--mmproj")
        del argv[index:index + 2]
    if depth:
        replace_flag(argv, "-m", MERGED)
    replace_flag(argv, "--host", "127.0.0.1")
    replace_flag(argv, "--port", "18080")
    argv += ["--alias", MODEL, "--spec-type", "mtp" if depth else "none"]
    if depth:
        argv += ["--spec-draft-n-max", str(depth)]
    if no_mmap:
        argv = [arg for arg in argv if arg != "--mmap"]
        argv += ["--no-mmap"]
    return argv


def profile_argv(original: list[str], profile: str) -> list[str]:
    argv = list(original)
    aliases = {
        "-b": ("-b", "--batch-size"), "-ub": ("-ub", "--ubatch-size"),
        "--threads": ("-t", "--threads"), "--threads-batch": ("-tb", "--threads-batch"),
    }
    for flag, value in PARAMETER_PROFILES[profile].items():
        existing = next((item for item in aliases.get(flag, (flag,)) if item in argv), None)
        if existing:
            replace_flag(argv, existing, value)
        else:
            argv += [flag, value]
    return argv


def fixture_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\0" + b"\xff\0\0" * 128 + b"\0\0\xff" * 128
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 128, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * 128))
        + chunk(b"IEND", b"")
    )


def wait_health(client, base: str, seconds: int = 180, test_unit: str | None = None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            response = client.get(base + "/health", timeout=5)
            if response.status_code == 200:
                slots = client.get(base + "/slots", timeout=5).json()
                if len(slots) == 1 and slots[0]["n_ctx"] == 262144:
                    return slots
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        if test_unit and ssh("systemctl", "is-failed", test_unit, check=False).strip() == "failed":
            raise RuntimeError("MTP test service failed during startup")
        time.sleep(2)
    raise TimeoutError("service did not become healthy with a single 256K slot")


def smoke(client, base: str, output: Path, *, include_images: bool = True) -> dict:
    output.mkdir(exist_ok=False)
    checks = []

    def call(name, messages, extra=None):
        body = {
            "model": MODEL, "messages": messages, "temperature": 0,
            "max_tokens": 128, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
            **(extra or {}),
        }
        record = {"request_id": uuid4().hex, "request": body, "passed": False}
        started = time.monotonic()
        try:
            response = client.post(
                base + "/v1/chat/completions", json=body,
                headers={"X-Request-ID": record["request_id"]}, timeout=45,
            )
            record.update({"status": response.status_code, "response": response.text})
            response.raise_for_status()
            value = response.json()
            return value["choices"][0]["message"], record
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            return None, record
        finally:
            record["seconds"] = time.monotonic() - started

    history = []
    for index, (question, expected) in enumerate((
        ("Remember marker BENCH_739216 and number 17. Reply only with the marker.", "BENCH_739216"),
        ("What marker did I give you? Reply with only the marker.", "BENCH_739216"),
        ("Add five to the number I asked you to remember. Reply only with the integer.", "22"),
        ("Now reply with the original marker only.", "BENCH_739216"),
    ), 1):
        history.append({"role": "user", "content": question})
        message, record = call(f"text-{index}", history)
        record["passed"] = bool(message and (message.get("content") or "").strip().strip('`".') == expected)
        save(output / f"text-{index}.json", record)
        checks.append({"case": f"text-{index}", "passed": record["passed"]})
        if message is None:
            break
        history.append(message)

    tool = {
        "type": "function", "function": {
            "name": "lookup_marker", "description": "Look up the marker for a location.",
            "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
        },
    }
    message, record = call("tool", [{"role": "user", "content": "Use lookup_marker for harbor."}], {
        "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "lookup_marker"}},
    })
    calls = message.get("tool_calls", []) if message else []
    try:
        record["passed"] = (
            len(calls) == 1 and calls[0]["function"]["name"] == "lookup_marker"
            and json.loads(calls[0]["function"]["arguments"]) == {"location": "harbor"}
        )
    except (ValueError, KeyError, TypeError):
        record["passed"] = False
    save(output / "tool.json", record)
    checks.append({"case": "tool", "passed": record["passed"]})

    if not include_images:
        result = {
            "passed": len(checks) == 5 and all(item["passed"] for item in checks),
            "checks": checks, "image_checks": "not_requested_text_only",
        }
        save(output / "report.json", result)
        return result

    image = "data:image/png;base64," + base64.b64encode(fixture_png()).decode()
    history = [{"role": "user", "content": [
        {"type": "text", "text": "Identify the dominant color in each half of this image. Return JSON with left and right."},
        {"type": "image_url", "image_url": {"url": image}},
    ]}]
    message, record = call("image", history, {
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "colors", "strict": True, "schema": {
                "type": "object",
                "properties": {key: {"type": "string", "enum": ["red", "blue", "green", "yellow"]} for key in ("left", "right")},
                "required": ["left", "right"], "additionalProperties": False,
            },
        }},
    })
    try:
        record["passed"] = bool(message and json.loads(message["content"]) == {"left": "red", "right": "blue"})
    except (ValueError, KeyError, TypeError):
        record["passed"] = False
    save(output / "image.json", record)
    checks.append({"case": "image", "passed": record["passed"]})
    if message:
        history += [message, {"role": "user", "content": "What was the color on the right? Reply only with the color name."}]
        message, record = call("image-followup", history)
        record["passed"] = bool(message and (message.get("content") or "").strip().lower().strip('`".') == "blue")
        save(output / "image-followup.json", record)
        checks.append({"case": "image-followup", "passed": record["passed"]})
    result = {"passed": len(checks) == 7 and all(item["passed"] for item in checks), "checks": checks}
    save(output / "report.json", result)
    return result


def long_retrieval(client, fixture_path: Path, output: Path) -> dict:
    fixture = json.loads(fixture_path.read_text())
    records = []
    for line in fixture["initial_messages"][1]["content"].splitlines():
        match = re.fullmatch(
            r"record (\d{6}): code ([0-9a-f]{12}); quantity (\d+); state (ready|pending|review)\.",
            line,
        )
        if match:
            key, code, quantity, state = match.groups()
            records.append((key, {"code": code, "quantity": int(quantity), "state": state}))
    if len(records) < 3:
        raise ValueError("long retrieval requires at least three synthetic records")
    expected = dict(records[index] for index in (0, len(records) // 2, len(records) - 1))
    messages = json.loads(json.dumps(fixture["turns"][-1]["messages"]))
    messages[-1]["content"] = (
        "Retrieve the exact code, quantity, and state from the original records with IDs "
        + ", ".join(expected) + ". Return only the requested JSON. Do not estimate or summarize."
    )
    item_schema = {
        "type": "object", "properties": {
            "code": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
            "quantity": {"type": "integer", "minimum": 0, "maximum": 9999},
            "state": {"type": "string", "enum": ["ready", "pending", "review"]},
        }, "required": ["code", "quantity", "state"], "additionalProperties": False,
    }
    request = {
        "model": MODEL, "messages": messages, "temperature": 0, "max_tokens": 256,
        "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "record_retrieval", "strict": True, "schema": {
                "type": "object", "properties": {key: item_schema for key in expected},
                "required": list(expected), "additionalProperties": False,
            },
        }},
    }
    record = {"request_id": uuid4().hex, "request": request, "expected": expected, "passed": False}
    started = time.monotonic()
    try:
        response = client.post(
            LOCAL_TEST + "/v1/chat/completions", json=request,
            headers={"X-Request-ID": record["request_id"]}, timeout=1800,
        )
        record.update({"status": response.status_code, "response": response.text})
        response.raise_for_status()
        choice = response.json()["choices"][0]
        record["passed"] = (
            choice["finish_reason"] == "stop"
            and json.loads(choice["message"]["content"]) == expected
        )
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["seconds"] = time.monotonic() - started
    save(output, record)
    return {"passed": record["passed"], "request_id": record["request_id"], "seconds": record["seconds"]}


def text_matrix(client, original: list[str], prefix: str, output: Path, report: dict, *,
                depths=(0, 2, 4), prompt_tokens=4096, workloads=("analysis", "code"),
                window_seconds=540, long_semantic_check=False, test_binary=None,
                no_mmap=False, startup_timeout=180, parameter_screen=False,
                runtime_profile="baseline") -> None:
    deadline = time.monotonic() + window_seconds
    started = int(time.time())
    benchmark = str(Path(__file__).with_name("benchmark-cerebellum-mtp.py"))
    workloads = [(name, {"analysis": 20260904, "code": 20260905, "operations": 20260906}[name])
                 for name in workloads]
    report["variants"] = []
    report["test_units"] = []
    def check_competitor() -> None:
        journal = competitor_journal(started)
        report["competitor_journal"] = journal
        if has_competitor_activity(journal):
            raise RuntimeError("concurrent translation loading/inference invalidates the matrix")

    variants = (
        [(name, 0, name) for name in ("baseline", "cache", "batch", "threads")]
        if parameter_screen else
        [("baseline" if depth == 0 else f"mtp{depth}", depth, runtime_profile) for depth in depths]
    )
    for index, (name, depth, profile) in enumerate(variants):
        check_competitor()
        if deadline - time.monotonic() < 90:
            raise TimeoutError("remaining short-test window is insufficient; restoring service")
        directory = output / name
        directory.mkdir(exist_ok=False)
        unit = prefix + "-" + name
        argv = profile_argv(text_argv(original, depth, no_mmap=no_mmap), profile)
        if test_binary:
            argv[0] = test_binary
        variant = {"name": name, "depth": depth, "profile": profile, "unit": unit, "argv": argv,
                   "passed": False, "groups": []}
        report["variants"].append(variant)
        report["test_units"].append(unit)
        phase_started = int(time.time())
        load_started = time.monotonic()
        try:
            # Every trial expires before the independent original-service
            # recovery timer, including trials started late in the matrix.
            lifetime = max(1, int(deadline - time.monotonic()))
            ssh(
                "sudo", "-n", "systemd-run", "--unit=" + unit, "--uid=agx",
                "--property=Restart=no", f"--property=RuntimeMaxSec={lifetime}",
                "--setenv=LD_LIBRARY_PATH=" + str(Path(argv[0]).parent), *argv,
            )
            wait_health(client, LOCAL_TEST, min(startup_timeout, lifetime), test_unit=unit)
            variant["startup_seconds"] = time.monotonic() - load_started
            props = client.get(LOCAL_TEST + "/props")
            props.raise_for_status()
            modalities = props.json().get("modalities", {})
            if modalities.get("vision") is not False:
                raise RuntimeError("trial has not confirmed that the vision projector is unloaded")
            save(directory / "modalities.json", modalities)
            print(f"{name}: ready after {variant['startup_seconds']:.1f}s; workloads="
                  + ",".join(workload for workload, _ in workloads), flush=True)
            for workload, seed in workloads:
                remaining = int(deadline - time.monotonic())
                if remaining < 30:
                    raise TimeoutError("short-test deadline reached")
                command = [
                    sys.executable, benchmark, "record" if index == 0 else "replay",
                    "--base-url", LOCAL_TEST, "--model", MODEL,
                    "--spec-depth", str(depth), "--output", str(directory / workload),
                    "--request-timeout", str(60 if prompt_tokens == 4096 else min(1800, remaining)), "--execute",
                ]
                if index == 0:
                    command += ["--prompt-tokens", str(prompt_tokens), "--workload", workload, "--seed", str(seed)]
                else:
                    command += ["--fixture", str(output / "baseline" / workload / "fixture.json")]
                with (directory / (workload + ".log")).open("x") as log:
                    process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                             timeout=min(150, remaining) if prompt_tokens == 4096 else remaining)
                path = directory / workload / "report.json"
                group = json.loads(path.read_text()) if path.exists() else {"passed": False, "error": "missing benchmark report"}
                variant["groups"].append(group)
                check_competitor()
                if process.returncode or not group["passed"]:
                    raise RuntimeError(f"{name}/{workload} failed; stopping the matrix")
                print(
                    f"{name}/{workload}: warm decode TPS = "
                    + ", ".join(f"{row['decode_tps']:.2f}" for row in group["turns"][1:]),
                    flush=True,
                )
                if long_semantic_check:
                    if deadline - time.monotonic() < 90:
                        raise TimeoutError("insufficient time for long semantic check")
                    retrieval = long_retrieval(
                        client, output / "baseline" / workload / "fixture.json",
                        directory / (workload + "-retrieval.json"),
                    )
                    variant.setdefault("long_retrieval", {})[workload] = retrieval
                    check_competitor()
                    print(f"{name}/{workload}: long retrieval = {retrieval['passed']}", flush=True)
                    if not retrieval["passed"]:
                        raise RuntimeError(f"{name}/{workload} long retrieval failed")
            variant["smoke"] = smoke(client, LOCAL_TEST, directory / "smoke", include_images=False)
            variant["memory_after"] = ssh("free", "-b")
            variant["telemetry_after"] = ssh("timeout", "3", "tegrastats", "--interval", "1000", check=False)
            check_competitor()
            variant["passed"] = all(group["passed"] for group in variant["groups"]) and variant["smoke"]["passed"]
            if not variant["passed"]:
                raise RuntimeError(f"{name} semantic/tool checks failed")
        except BaseException as exc:
            variant["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stop_trial(unit)
            variant["shutdown"] = shutdown_status(unit)
            variant["inference_passed"] = variant["passed"]
            variant["passed"] = variant["passed"] and variant["shutdown"]["passed"]
            variant["journal"] = ssh("journalctl", "-u", unit, "--no-pager", "-n", "3000", check=False)
            kernel = ssh("sudo", "-n", "journalctl", "-k", "--since=@" + str(phase_started),
                         "--no-pager", check=False)
            variant["kernel_events"] = [line for line in kernel.splitlines()
                                        if any(term in line.lower() for term in ("nvme", "nvrm", "oom", "out of memory"))]
            save(directory / "variant.json", variant)
            if variant["inference_passed"] and not variant["shutdown"]["passed"]:
                raise RuntimeError(f"{name} failed clean shutdown; backend repair required before more tests")
    for name, depth, _ in variants[1:]:
        command = [sys.executable, benchmark, "compare", "--baseline"]
        command += [str(output / "baseline" / workload / "report.json") for workload, _ in workloads]
        command += ["--candidate"]
        command += [str(output / name / workload / "report.json") for workload, _ in workloads]
        command += ["--output", str(output / f"comparison-{name}.json")]
        subprocess.run(command, check=True, timeout=15)
    report["passed"] = all(variant["passed"] for variant in report["variants"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    parser.add_argument("--text-matrix", action="store_true")
    parser.add_argument("--depths", type=int, nargs="+", choices=(0, 1, 2, 4), default=[0, 2, 4])
    parser.add_argument("--prompt-tokens", type=int, choices=(4096, 42000, 128000, 260000), default=4096)
    parser.add_argument("--workloads", nargs="+", choices=("analysis", "code", "operations"), default=["analysis", "code"])
    parser.add_argument("--window-seconds", type=int, default=540)
    parser.add_argument("--long-semantic-check", action="store_true")
    parser.add_argument("--test-binary")
    parser.add_argument("--no-mmap", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=180)
    parser.add_argument("--parameter-screen", action="store_true")
    parser.add_argument("--runtime-profile", choices=tuple(PARAMETER_PROFILES), default="baseline")
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("requires explicit authorization for the short maintenance window")
    if args.parameter_screen and (not args.text_matrix or args.runtime_profile != "baseline"):
        parser.error("parameter screen requires text-matrix and its own fixed profiles")
    if len(args.depths) < 2 or args.depths[0] != 0 or len(set(args.depths)) != len(args.depths):
        parser.error("depths must begin with baseline 0 and contain unique candidates")
    if not 540 <= args.window_seconds <= 7200:
        parser.error("window must be between 540 and 7200 seconds")
    if not 30 <= args.startup_timeout <= 900:
        parser.error("startup timeout must be between 30 and 900 seconds")
    if len(set(args.workloads)) != len(args.workloads):
        parser.error("workloads must be unique")
    os.umask(0o077)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if not args.text_matrix and not args.baseline:
        parser.error("requires --baseline or --text-matrix")
    baseline = Path(args.baseline).resolve() if args.baseline else None
    if not args.text_matrix:
        base_report = json.loads((baseline / "report.json").read_text())
        if not base_report["passed"] or base_report["spec_depth"] != 0:
            parser.error("requires a completed original-model baseline")
    report = {
        "passed": False, "restored": False,
        "scope": "text-only-matrix" if args.text_matrix else "4k-mtp2-quick-screen-only",
        "matrix_parameters": {"depths": args.depths, "prompt_tokens": args.prompt_tokens,
                              "workloads": args.workloads, "window_seconds": args.window_seconds,
                              "long_semantic_check": args.long_semantic_check,
                              "no_mmap": args.no_mmap, "startup_timeout": args.startup_timeout,
                              "parameter_screen": args.parameter_screen, "runtime_profile": args.runtime_profile},
    }
    test_unit = "agx-cerebellum-mtp-quick-" + uuid4().hex[:8]
    recovery = test_unit + "-recovery"
    stopped = False
    recovery_armed = False
    tunnel = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
        try:
            key = subprocess.check_output([
                "docker", "exec", "1panel-ai-router-router-api-local-1", "python", "-c",
                'import os;print(os.environ["AI_ROUTER_ADMIN_KEY"])',
            ], text=True).strip()
            headers = {"Authorization": "Bearer " + key}
            def endpoint():
                response = client.get(CONTROL + "/api/endpoints", headers=headers)
                response.raise_for_status()
                return next(row for row in response.json()["endpoints"] if row["endpoint"]["id"] == ENDPOINT)
            before = endpoint()
            if before["endpoint"]["enabled"]:
                raise RuntimeError("AGX endpoint must already be disabled for the confirmed window")
            report["routing_before"] = before
            original_unit = ssh("systemctl", "cat", ORIGINAL)
            report["original_unit_sha256"] = hashlib.sha256(original_unit.encode()).hexdigest()
            save(output / "original-unit.json", {"content": original_unit})
            pid = int(ssh("systemctl", "show", ORIGINAL, "--value", "-p", "MainPID").strip())
            argv = json.loads(ssh(
                "python3", "-c",
                f"import json,pathlib;print(json.dumps(pathlib.Path('/proc/{pid}/cmdline').read_bytes().decode().rstrip(chr(0)).split(chr(0))))",
            ))
            if "/data/models/" + MODEL not in argv or argv[argv.index("-c") + 1] != "262144":
                raise RuntimeError("original service differs from the audited model/context")
            report["original_argv"] = argv
            if args.test_binary:
                if not args.text_matrix or not args.test_binary.startswith(ARTIFACTS + "/"):
                    raise ValueError("test binary must be an isolated artifact used only in matrix mode")
                report["test_binary_sha256"] = ssh("sha256sum", args.test_binary).split()[0]
            report["prepare"] = json.loads(ssh("cat", ARTIFACTS + "/prepare-report.json"))
            if not report["prepare"]["passed"]:
                raise RuntimeError("merged artifact verification failed")
            occupied = ssh("ss", "-ltn", "sport = :18080").splitlines()
            if len(occupied) != 1:
                raise RuntimeError("AGX test port is occupied")
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", 18081))
            for _ in range(3):
                slots = wait_health(client, DIRECT, 10)
                if slots[0].get("is_processing"):
                    raise RuntimeError("AGX has an active request; refusing to stop it")
                time.sleep(1)
            if args.text_matrix:
                report["competitor_preflight"] = competitor_journal(int(time.time()) - 60)
                if has_competitor_activity(report["competitor_preflight"]):
                    raise RuntimeError("translation service changed or processed requests in the last minute")
            report["baseline_smoke"] = smoke(client, DIRECT, output / "baseline-smoke")
            print("baseline semantic/vision checks: " + json.dumps(report["baseline_smoke"]), flush=True)
            report["maintenance_started_at"] = time.time()
            # This independent timer can recover the original process even if
            # the local SSH/client process disappears. The trial expires first.
            recovery_delay = f"{args.window_seconds + 120}s" if args.text_matrix else "11m"
            ssh("sudo", "-n", "systemd-run", "--unit=" + recovery, "--on-active=" + recovery_delay,
                "/usr/bin/systemctl", "start", ORIGINAL)
            recovery_armed = True
            stopped = True
            ssh("sudo", "-n", "systemctl", "stop", ORIGINAL, timeout=45)
            tunnel = subprocess.Popen([
                "ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-N",
                "-L", "127.0.0.1:18081:127.0.0.1:18080", "agx",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if args.text_matrix:
                text_matrix(client, argv, test_unit, output, report, depths=args.depths,
                            prompt_tokens=args.prompt_tokens, workloads=args.workloads,
                            window_seconds=args.window_seconds, long_semantic_check=args.long_semantic_check,
                            test_binary=args.test_binary, no_mmap=args.no_mmap,
                            startup_timeout=args.startup_timeout, parameter_screen=args.parameter_screen,
                            runtime_profile=args.runtime_profile)
            else:
                test_argv = list(argv)
                replace_flag(test_argv, "-m", MERGED)
                replace_flag(test_argv, "--host", "127.0.0.1")
                replace_flag(test_argv, "--port", "18080")
                test_argv += ["--alias", MODEL, "--spec-type", "mtp", "--spec-draft-n-max", "2"]
                report["test_argv"] = test_argv
                report["test_unit"] = test_unit
                ssh(
                    "sudo", "-n", "systemd-run", "--unit=" + test_unit, "--uid=agx",
                    "--property=Restart=no", "--property=RuntimeMaxSec=480",
                    "--setenv=LD_LIBRARY_PATH=" + str(Path(argv[0]).parent), *test_argv,
                )
                wait_health(client, LOCAL_TEST, 180, test_unit=test_unit)
                print("MTP2 loaded; starting the identical four-turn replay", flush=True)
                with (output / "benchmark.log").open("x") as log:
                    result = subprocess.run([
                        sys.executable, str(Path(__file__).with_name("benchmark-cerebellum-mtp.py")),
                        "replay", "--base-url", LOCAL_TEST, "--model", MODEL,
                        "--spec-depth", "2", "--fixture", str(baseline / "fixture.json"),
                        "--output", str(output / "mtp2"), "--request-timeout", "60", "--execute",
                    ], stdout=log, stderr=subprocess.STDOUT, timeout=180)
                report["benchmark_returncode"] = result.returncode
                if (output / "mtp2" / "report.json").exists():
                    report["mtp2"] = json.loads((output / "mtp2" / "report.json").read_text())
                report["mtp2_smoke"] = smoke(client, LOCAL_TEST, output / "mtp2-smoke")
                report["passed"] = bool(report.get("mtp2", {}).get("passed") and report["mtp2_smoke"]["passed"])
        except BaseException as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if stopped:
                try:
                    for unit in report.get("test_units", [test_unit]):
                        stop_trial(unit)
                    if not args.text_matrix:
                        report["test_journal"] = ssh("journalctl", "-u", test_unit, "--no-pager", "-n", "3000", check=False)
                        report["test_shutdown"] = shutdown_status(test_unit)
                        report["passed"] = report["passed"] and report["test_shutdown"]["passed"]
                    if tunnel:
                        tunnel.terminate()
                        tunnel.wait(timeout=10)
                    if ssh("systemctl", "cat", ORIGINAL) != original_unit:
                        raise RuntimeError("original unit changed concurrently; recovery timer remains armed")
                    ssh("sudo", "-n", "systemctl", "start", ORIGINAL, timeout=45)
                    wait_health(client, DIRECT, max(180, args.startup_timeout))
                    report["restoration_smoke"] = smoke(client, DIRECT, output / "restoration-smoke")
                    report["routing_after"] = endpoint()
                    report["restored"] = (
                        report["restoration_smoke"]["passed"]
                        and report["routing_after"]["endpoint"]["enabled"] == before["endpoint"]["enabled"]
                        and report["routing_after"]["endpoint"]["auto_candidate"] == before["endpoint"]["auto_candidate"]
                    )
                    if recovery_armed:
                        ssh("sudo", "-n", "systemctl", "stop", recovery + ".timer", check=False)
                except BaseException as exc:
                    report["restore_error"] = f"{type(exc).__name__}: {exc}"
            report["finished_at"] = time.time()
            if report.get("maintenance_started_at"):
                report["maintenance_seconds"] = report["finished_at"] - report["maintenance_started_at"]
            save(output / "report.json", report)
            print(json.dumps({
                key: value for key, value in report.items()
                if key in ("passed", "restored", "error", "restore_error", "maintenance_seconds", "benchmark_returncode")
            }), flush=True)
    return 0 if report["restored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
