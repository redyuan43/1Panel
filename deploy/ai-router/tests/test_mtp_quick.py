from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zlib
import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate-agx-mtp-quick.py"
SPEC = importlib.util.spec_from_file_location("mtp_quick", PATH)
assert SPEC and SPEC.loader
quick = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quick)


def test_image_fixture_has_known_left_and_right_colors():
    png = quick.fixture_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    pixels = b""
    while offset < len(png):
        size = int.from_bytes(png[offset:offset + 4], "big")
        if png[offset + 4:offset + 8] == b"IDAT":
            pixels += png[offset + 8:offset + 8 + size]
        offset += 12 + size
    row = zlib.decompress(pixels)[:769]
    assert row[0] == 0
    assert row[1:385] == b"\xff\0\0" * 128
    assert row[385:] == b"\0\0\xff" * 128


@pytest.mark.parametrize("depth", [0, 1, 2, 4])
def test_text_variants_preserve_context_without_projector(depth):
    original = ["/test/server", "-m", "/data/models/" + quick.MODEL,
                "--mmproj", "/data/projector.gguf", "-c", "262144",
                "--host", "0.0.0.0", "--port", "8080"]
    argv = quick.text_argv(original, depth)
    assert "--mmproj" in original
    assert "--mmproj" not in argv
    assert "/data/projector.gguf" not in argv
    assert argv[argv.index("-c") + 1] == "262144"
    assert argv[argv.index("-m") + 1] == (quick.MERGED if depth else "/data/models/" + quick.MODEL)
    assert argv[argv.index("--spec-type") + 1] == ("mtp" if depth else "none")
    if depth:
        assert argv[argv.index("--spec-draft-n-max") + 1] == str(depth)


def test_no_mmap_changes_only_the_trial_load_option():
    original = ["server", "-m", "/model", "--host", "0.0.0.0", "--port", "8080", "--mmap"]
    argv = quick.text_argv(original, 1, no_mmap=True)
    assert "--mmap" in original
    assert "--mmap" not in argv
    assert "--no-mmap" in argv


def test_trial_stop_allows_slow_cuda_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(quick, "ssh", lambda *args, **kwargs: calls.append((args, kwargs)))
    quick.stop_trial("test.service")
    assert calls == [(("sudo", "-n", "systemctl", "stop", "test.service"),
                      {"timeout": 120, "check": False})]


@pytest.mark.parametrize("profile", ["baseline", "cache", "batch", "threads", "combined"])
def test_parameter_profiles_preserve_model_contract(profile):
    original = ["server", "-m", "/model", "-c", "262144", "-ngl", "99", "--parallel", "1",
                "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                "-b", "2048", "-ub", "512", "-t", "12", "-tb", "12"]
    argv = quick.profile_argv(original, profile)
    for flag in ("-m", "-c", "-ngl", "--parallel", "--cache-type-k", "--cache-type-v"):
        assert argv[argv.index(flag) + 1] == original[original.index(flag) + 1]
    assert original[original.index("-t") + 1] == "12"
    assert argv[argv.index("-t") + 1] == ("6" if profile in ("threads", "combined") else "12")
    assert argv[argv.index("-b") + 1] == ("1024" if profile in ("batch", "combined") else "2048")
    assert argv[argv.index("-ub") + 1] == ("256" if profile in ("batch", "combined") else "512")
    if profile != "baseline":
        assert argv[argv.index("--cache-ram") + 1] == "4096"
        assert argv[argv.index("--ctx-checkpoints") + 1] == "8"


def test_parameter_matrix_replays_all_candidates_even_without_mtp(tmp_path, monkeypatch):
    commands = []
    original = ["server", "-m", "/model", "--host", "0.0.0.0", "--port", "8080"]

    def run(command, **kwargs):
        commands.append(command)
        if command[2] != "compare":
            directory = Path(command[command.index("--output") + 1])
            directory.mkdir()
            (directory / "report.json").write_text(json.dumps({
                "passed": True, "turns": [{"decode_tps": 35}] * 4,
            }))
        return SimpleNamespace(returncode=0)

    client = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"modalities": {"vision": False}},
    ))
    monkeypatch.setattr(quick, "ssh", lambda *args, **kwargs: "")
    monkeypatch.setattr(quick, "wait_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(quick, "shutdown_status", lambda *args: {"passed": True})
    monkeypatch.setattr(quick, "smoke", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(quick.subprocess, "run", run)
    report = {}
    quick.text_matrix(client, original, "trial", tmp_path, report,
                      parameter_screen=True, workloads=("analysis",))
    assert report["passed"]
    assert [command[2] for command in commands] == ["record", "replay", "replay", "replay",
                                                   "compare", "compare", "compare"]


def test_text_smoke_never_sends_images(tmp_path):
    messages = iter([
        {"content": "BENCH_739216"}, {"content": "BENCH_739216"},
        {"content": "22"}, {"content": "BENCH_739216"},
        {"tool_calls": [{"function": {"name": "lookup_marker", "arguments": '{"location":"harbor"}'}}]},
    ])

    class Client:
        def post(self, url, *, json, **kwargs):
            assert all(isinstance(item["content"], str) for item in json["messages"])
            value = {"choices": [{"message": next(messages)}]}
            return SimpleNamespace(
                status_code=200, text=str(value), raise_for_status=lambda: None, json=lambda: value,
            )

    assert quick.smoke(Client(), "http://test", tmp_path / "smoke", include_images=False)["passed"]


@pytest.mark.parametrize("matrix", [False, True])
def test_startup_failure_restores_original_without_enabling_routing(tmp_path, monkeypatch, matrix):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "report.json").write_text(json.dumps({"passed": True, "spec_depth": 0}))
    output = tmp_path / "trial"
    calls = []
    argv = ["/test/llama-server", "-m", "/data/models/" + quick.MODEL,
            "-c", "262144", "--host", "0.0.0.0", "--port", "8080"]
    row = {"endpoint": {"id": quick.ENDPOINT, "enabled": False, "auto_candidate": True}}

    def ssh(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("systemctl", "cat"):
            return "original unit"
        if args[:2] == ("systemctl", "show"):
            return "99"
        if args[0] == "python3":
            return json.dumps(argv)
        if args[0] == "cat":
            return '{"passed": true}'
        if args[0] == "ss":
            return "header\n"
        if args[:3] == ("sudo", "-n", "systemd-run") and not any(arg.startswith("--on-active=") for arg in args):
            raise RuntimeError("simulated MTP startup failure")
        return ""

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"endpoints": [row]})

    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, *args):
            pass
        def setsockopt(self, *args):
            pass

    monkeypatch.setattr(quick, "ssh", ssh)
    monkeypatch.setattr(quick.httpx, "Client", Client)
    monkeypatch.setattr(quick.socket, "socket", lambda: Socket())
    monkeypatch.setattr(quick.signal, "signal", lambda *args: None)
    monkeypatch.setattr(quick.time, "sleep", lambda *args: None)
    monkeypatch.setattr(quick.subprocess, "check_output", lambda *args, **kwargs: "test-key")
    monkeypatch.setattr(quick.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        terminate=lambda: None, wait=lambda **kwargs: None,
    ))
    monkeypatch.setattr(quick, "wait_health", lambda *args, **kwargs: [{"is_processing": False}])
    monkeypatch.setattr(quick, "smoke", lambda *args: {"passed": True})
    monkeypatch.setattr(quick.sys, "argv", [
        "quick", "--baseline", str(baseline), "--output", str(output), "--execute",
    ] + (["--text-matrix"] if matrix else []))
    assert quick.main() == 0
    report = json.loads((output / "report.json").read_text())
    assert report["restored"]
    assert not report["passed"]
    assert "simulated MTP startup failure" in report["error"]
    assert ("sudo", "-n", "systemctl", "start", quick.ORIGINAL) in calls
    assert any(call[-1].endswith(".timer") for call in calls)
    assert report["routing_after"]["endpoint"]["enabled"] is False


def test_matrix_rejects_concurrent_translation_before_start(tmp_path, monkeypatch):
    monkeypatch.setattr(quick, "ssh", lambda *args, **kwargs: "slot: processing task")
    report = {}
    with pytest.raises(RuntimeError, match="concurrent translation"):
        quick.text_matrix(None, [], "test", tmp_path, report)
    assert report["test_units"] == []


@pytest.mark.parametrize("message", ["processing task", "load_tensors: CUDA0",
                                     "Started AGX", "Stopping AGX", "Stopped AGX"])
def test_competitor_inference_and_lifecycle_changes_invalidate_comparison(message):
    assert quick.has_competitor_activity(message)
    assert not quick.has_competitor_activity("-- No entries --")


@pytest.mark.parametrize("case,passed", [("correct", True), ("wrong", False), ("truncated", False)])
def test_long_retrieval_checks_three_positions_and_normal_eos(tmp_path, case, passed):
    content = "\n".join(f"record {i:06d}: code {i:012x}; quantity {i}; state ready." for i in range(5))
    initial = [{"role": "system", "content": "system"}, {"role": "user", "content": content}]
    messages = initial + [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "followup"}]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"initial_messages": initial, "turns": [{"messages": messages}]}))
    expected = {f"{i:06d}": {"code": f"{i:012x}", "quantity": i, "state": "ready"} for i in (0, 2, 4)}
    if case == "wrong":
        expected["000002"]["quantity"] = 99
    value = {"choices": [{"finish_reason": "length" if case == "truncated" else "stop",
                          "message": {"content": json.dumps(expected)}}]}

    class Client:
        def post(self, url, *, json, **kwargs):
            assert "ignore_eos" not in json
            assert json["messages"][:-1] == messages[:-1]
            assert json["max_tokens"] == 256
            assert set(json["response_format"]["json_schema"]["schema"]["required"]) == {"000000", "000002", "000004"}
            return SimpleNamespace(status_code=200, text=str(value), json=lambda: value,
                                   raise_for_status=lambda: None)

    assert quick.long_retrieval(Client(), fixture, tmp_path / "result.json")["passed"] is passed


@pytest.mark.parametrize("result,state,passed", [
    ("success", "inactive", True), ("core-dump", "failed", False),
    ("timeout", "failed", False), ("success", "active", False),
])
def test_shutdown_requires_clean_inactive_service(monkeypatch, result, state, passed):
    monkeypatch.setattr(quick, "ssh", lambda *args, **kwargs:
                        f"Result={result}\nMainPID=0\nActiveState={state}\n")
    assert quick.shutdown_status("test.service")["passed"] is passed
