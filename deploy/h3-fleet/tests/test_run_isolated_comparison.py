import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile

import httpx
import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_isolated_comparison as runner


def test_fleet_uses_discovered_private_binding():
    assert runner.FLEET == "http://ivan-ms-7b17.taild500c8.ts.net:8789"


def test_collect_preserves_upstream_error_without_prompt_or_traceback():
    driver = object.__new__(runner.Driver)
    driver.report = {}
    payload = {"node_id": "10", "node_type": "SamplerCustomAdvanced",
               "exception_type": "torch.OutOfMemoryError", "exception_message": "CUDA exhausted",
               "traceback": ["private diagnostic"], "current_inputs": {"prompt": "private prompt"}}
    with pytest.raises(RuntimeError, match="torch.OutOfMemoryError at node 10"):
        driver.collect({}, {"status": {"messages": [["execution_error", payload]]}})
    assert set(driver.report["upstream_error"]) == {
        "node_id", "node_type", "exception_type", "exception_message"}


def test_swap_recovery_keeps_raw_sample_and_growth_gate(monkeypatch):
    sample = {"swap_used_bytes": 3 * 1024**3, "cgroup_swap_bytes": 2 * 1024**3}
    limits = {"swap_recovery_mode": "stable_idle_exclusive_fast_only", "swap_hard_limit_gib": 8}
    recovery = {"host_bytes": 2 * 1024**3, "cgroup_bytes": 2 * 1024**3}
    observed = []
    monkeypatch.setattr(runner, "safety_reason", lambda current, baseline, policy: observed.append(current))
    assert runner.isolated_safety_reason(sample, {}, limits, recovery) is None
    assert sample["swap_used_bytes"] == 3 * 1024**3
    assert observed[0]["swap_used_bytes"] == 1024**3
    assert runner.isolated_safety_reason(dict(sample, swap_used_bytes=9 * 1024**3), {}, limits, recovery) == "swap hard ceiling crossed"


@pytest.fixture
def graph():
    path = SCRIPTS.parent / "experiments/optimization-20260909/a_lightx2v/tests/baseline.json"
    workflow = json.loads(path.read_text())
    clock = next(node["inputs"] for node in workflow.values() if node["class_type"] == "MiniMaxH3DualClockSamplerT8")
    clock["shift_video"] = 6
    return workflow


@pytest.mark.parametrize("case,steps", [("A4", 4), ("A8", 8)])
def test_real_frozen_candidate(case, steps):
    path = Path("/home/ai/.local/state/h3-studio-ivan-production/deployment/optimization-20260909") / (case + "-candidate.json")
    if not path.exists():
        pytest.skip("operator's frozen candidate is not installed on this test host")
    raw = path.read_bytes()
    shape = runner.validate_workflow(json.loads(raw), case)
    assert (shape["steps"], shape["shift_video"], shape["shift_audio"]) == (steps, 6, 3)
    assert (shape["width"], shape["height"], shape["length"]) == (480, 864, 362)
    assert len(hashlib.sha256(raw).hexdigest()) == 64


def test_shift_is_frozen_and_other_recipes_unchanged(graph):
    original = copy.deepcopy(graph)
    assert runner.validate_workflow(graph, "A4")["shift_video"] == 6
    assert graph == original
    with pytest.raises(ValueError, match="shift mismatch"):
        runner.validate_workflow(graph, "C0")
    graph["7"]["inputs"]["shift_video"] = 12
    assert runner.validate_workflow(graph, "C0")["shift_video"] == 12
    with pytest.raises(ValueError, match="shift mismatch"):
        runner.validate_workflow(graph, "A4")


@pytest.fixture
def a4_realism_graph(graph, request):
    case = request.param
    graph["6"]["inputs"]["prompt"] = "r34l1sm\n 共同粉色广告 IR\r\n原文保留 "
    graph["5"]["inputs"]["lora_name"] = runner.A4_LORA if case == "A4_C0" else f"offline-fixture-{case}.safetensors"
    return case, graph


@pytest.mark.parametrize("a4_realism_graph", ["A4_C0", "A4_C05", "A4_C1"], indirect=True)
def test_exact_a4_realism_recipes_preserve_graph_and_four_steps(a4_realism_graph):
    case, graph = a4_realism_graph
    original = copy.deepcopy(graph)
    assert runner.validate_workflow(graph, case) == {
        "width": 480, "height": 864, "length": 362, "fps": 24,
        "steps": 4, "shift_video": 6, "shift_audio": 3, "save_node": "13"}
    assert graph == original
    for identifier, field, value in (("7", "steps", 8), ("7", "shift_video", 12),
                                     ("7", "shift_audio", 6), ("7", "sampler_name", "euler"),
                                     ("7", "scheduler", "simple"), ("6", "length", 124),
                                     ("6", "width", 768), ("5", "strength_model", 0.5),
                                     ("4", "unet_name", "other.safetensors"),
                                     ("3", "clip_name", "other.safetensors"),
                                     ("1", "vae_name", "other.safetensors"),
                                     ("6", "prompt", "common prompt without trigger"),
                                     ("6", "prompt", "r34l1sm\n"),
                                     ("6", "prompt", "r34l1sm\nr34l1sm\ncommon")):
        changed = copy.deepcopy(graph)
        changed[identifier]["inputs"][field] = value
        with pytest.raises(ValueError):
            runner.validate_workflow(changed, case)


@pytest.mark.parametrize("case", ["A4_C", "A4_C00", "A4_C5", "A4_C0.5", "A4_C2", "A4_C10", "A4_C1_extra"])
def test_unapproved_a4_realism_names_are_rejected_even_with_legacy_shift(graph, case):
    for shift in (6, 12):
        graph["7"]["inputs"]["shift_video"] = shift
        with pytest.raises(ValueError, match="only exact"):
            runner.validate_workflow(graph, case)


@pytest.mark.parametrize("a4_realism_graph", ["A4_C0", "A4_C05", "A4_C1"], indirect=True)
def test_a4_realism_rejects_missing_or_stacked_bypass(a4_realism_graph):
    case, graph = a4_realism_graph
    missing = copy.deepcopy(graph)
    del missing["5"]
    missing["7"]["inputs"]["model"] = ["4", 0]
    with pytest.raises(ValueError, match="one Bypass"):
        runner.validate_workflow(missing, case)
    graph["extra"] = copy.deepcopy(graph["5"])
    with pytest.raises(ValueError):
        runner.validate_workflow(graph, case)


@pytest.mark.parametrize("a4_realism_graph", ["A4_C0", "A4_C05", "A4_C1"], indirect=True)
def test_a4_realism_rejects_wrong_known_artifacts(a4_realism_graph):
    case, graph = a4_realism_graph
    names = ["t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors",
             "c_realism_ema_people_fp32.safetensors", "h3-realism-people-t2v-i2v-r2v.safetensors"]
    names.append("offline-composition.safetensors" if case == "A4_C0" else runner.A4_LORA)
    for name in names:
        graph["5"]["inputs"]["lora_name"] = name
        with pytest.raises(ValueError):
            runner.validate_workflow(graph, case)


@pytest.mark.parametrize("a4_realism_graph", ["A4_C0", "A4_C05", "A4_C1"], indirect=True)
def test_a4_realism_preparation_for_18191_is_offline_and_frozen(a4_realism_graph, monkeypatch):
    case, graph = a4_realism_graph
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("no network during preparation"))
    with tempfile.TemporaryDirectory(prefix="a4-realism-", dir=SCRIPTS.parent / "tests") as directory:
        root = Path(directory)
        source = root / "source.json"
        source.write_text(json.dumps(graph))
        args = runner.parser().parse_args([
            "--workflow", str(source), "--workflow-sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
            "--case", case, "--run-id", "offline-realism", "--output-dir", str(root / "run"),
            "--isolated-unit", "offline-realism.service", "--isolated-url", "http://127.0.0.1:18191",
            "--isolated-gpu-uuid", "GPU-11111111-1111-1111-1111-111111111111"])
        assert runner.run(args) == 0
        prepared = json.loads((args.output_dir / "workflow.json").read_bytes())
        expected = copy.deepcopy(graph)
        expected["13"]["inputs"]["filename_prefix"] = f"video/h3-isolated/offline-realism/{case}"
        assert prepared == expected
        report = json.loads((args.output_dir / "report.json").read_bytes())
        assert report["case"] == case
        assert report["isolated_url"] == "http://127.0.0.1:18191"
        assert not report["submit_attempted"]
        assert runner.run(args) == 0


def test_hash_mismatch_precedes_any_network_or_output(graph, tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(graph))
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("network client must not be created"))
    args = SimpleNamespace(workflow=path, workflow_sha256="0" * 64)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        runner.run(args)


@pytest.fixture
def b8_graph():
    return json.loads((SCRIPTS.parent / "experiments/optimization-20260909/b_vdn/workflow.json").read_bytes())


@pytest.fixture
def d4_graph(graph):
    path = SCRIPTS.parent / "experiments/optimization-20260909/d_fasth3/prepare.py"
    spec = importlib.util.spec_from_file_location("d4_prepare_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workflow, _ = module.build_workflow(graph, "共同 prompt 原文", seed=42,
                                        filename_prefix="video/h3-comparison/D4-test")
    return workflow


def test_d4_delegates_to_strict_validator(d4_graph):
    assert runner.validate_workflow(d4_graph, "D4") == {
        "width": 480, "height": 864, "length": 362, "fps": 24,
        "steps": 4, "shift_video": 12, "shift_audio": 3, "save_node": "13"}
    for case in ("B8", "A4", "C1"):
        with pytest.raises(ValueError):
            runner.validate_workflow(d4_graph, case)
    d4_graph["10"]["inputs"]["sigmas"] = ["7", 2]
    with pytest.raises(ValueError, match="unaudited D4"):
        runner.validate_workflow(d4_graph, "D4")


def test_b8_delegates_to_exact_contract(b8_graph):
    original = copy.deepcopy(b8_graph)
    assert runner.validate_workflow(b8_graph, "B8") == {
        "width": 480, "height": 864, "length": 362, "fps": 24,
        "steps": 8, "shift_video": 12, "shift_audio": 3, "save_node": "13"}
    assert b8_graph == original
    b8_graph["6"]["inputs"]["prompt"] = "共同 prompt，保持原文。"
    b8_graph["8"]["inputs"]["noise_seed"] = 123
    b8_graph["13"]["inputs"]["filename_prefix"] = "video/test-B8"
    assert runner.validate_workflow(b8_graph, "B8")["steps"] == 8
    b8_graph["5"]["inputs"]["verify_hashes"] = False
    with pytest.raises(ValueError, match="unaudited B8"):
        runner.validate_workflow(b8_graph, "B8")


@pytest.mark.parametrize("case", [None, "A4", "A8", "C0", "C1"])
def test_b8_cannot_use_native_validation(b8_graph, case):
    with pytest.raises(ValueError, match="unknown/unaudited"):
        runner.validate_workflow(b8_graph, case)


def test_native_graph_cannot_be_labelled_b8(graph):
    with pytest.raises(ValueError):
        runner.validate_workflow(graph, "B8")


@pytest.mark.parametrize("isolated_url", [runner.ISOLATED, "http://127.0.0.1:18189"])
@pytest.mark.parametrize("case,namespace", [("B8", "h3-isolated"), ("D4", "h3-comparison")])
def test_candidate_preparation_is_offline_and_only_rewrites_prefix(b8_graph, d4_graph, monkeypatch, case, namespace, isolated_url):
    graph = b8_graph if case == "B8" else d4_graph
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("no network during preparation"))
    with tempfile.TemporaryDirectory(prefix="offline-b8-", dir=SCRIPTS.parent / "tests") as directory:
        root = Path(directory)
        source = root / "source.json"
        source.write_text(json.dumps(graph))
        raw = source.read_bytes()
        args = runner.parser().parse_args([
            "--workflow", str(source), "--workflow-sha256", hashlib.sha256(raw).hexdigest(),
            "--case", case, "--run-id", "offline-candidate", "--output-dir", str(root / "run"),
            "--isolated-unit", "offline-b8.service",
            "--isolated-url", isolated_url,
            "--isolated-gpu-uuid", "GPU-11111111-1111-1111-1111-111111111111"])
        assert runner.run(args) == 0
        report = json.loads((args.output_dir / "report.json").read_bytes())
        prepared = json.loads((args.output_dir / "workflow.json").read_bytes())
        expected = copy.deepcopy(graph)
        expected["13"]["inputs"]["filename_prefix"] = f"video/{namespace}/offline-candidate/{case}"
        assert prepared == expected
        assert report["shape"] == runner.validate_workflow(prepared, case)
        assert report["status"] == "prepared"
        assert report["isolated_url"] == isolated_url
        assert report["idle_wait_timeout"] == 180
        assert not report["submit_attempted"]
        assert source.read_bytes() == raw
        assert runner.run(args) == 0
        args.isolated_url = "http://127.0.0.1:18190"
        with pytest.raises(RuntimeError, match="frozen contract changed"):
            runner.run(args)
        args.isolated_url = isolated_url
        args.idle_wait_timeout = 1800
        with pytest.raises(RuntimeError, match="frozen contract changed"):
            runner.run(args)
        args.idle_wait_timeout = 180
        args.workflow_sha256 = "0" * 64
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            runner.run(args)


@pytest.mark.parametrize("case", ["B4", "B16", "D8", "D16"])
def test_other_b_and_d_cases_still_rejected(b8_graph, tmp_path, case):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(b8_graph))
    args = SimpleNamespace(workflow=source, workflow_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), case=case)
    with pytest.raises(ValueError, match="only audited B8"):
        runner.run(args)


class VirtualClock:
    def __init__(self):
        self.elapsed_ns = 0

    def monotonic(self):
        return self.elapsed_ns / 1_000_000_000

    def time(self):
        return 1_700_000_000 + self.monotonic()

    def sleep(self, seconds):
        self.elapsed_ns += round(seconds * 1_000_000_000)


@pytest.fixture(params=[18188, 18189])
def runtime(graph, tmp_path, monkeypatch, request):
    clock = VirtualClock()
    monkeypatch.setattr(runner, "time", clock)
    shape = runner.validate_workflow(graph, "A4")
    prefix = "video/h3-isolated/offline-A4/A4"
    graph[shape["save_node"]]["inputs"]["filename_prefix"] = prefix
    args = SimpleNamespace(output_dir=tmp_path, isolated_unit="h3-comparison-runtime-20260909.service",
                           isolated_url=f"http://127.0.0.1:{request.param}",
                           isolated_gpu_uuid="GPU-11111111-1111-1111-1111-111111111111",
                           timeout=0.04, idle_wait_timeout=180, lease_ttl=120, poll_interval=0.001,
                           cancel_timeout=0.004, free_timeout=0.004,
                           max_fast_vram_mib=1024, case="A4", run_id="offline-A4")
    isolated = {"MainPID": "2074565", "ControlGroup": runner.CGROUP + "/isolated.service"}
    services = [{"Id": "comfyui-h3@" + lane + ".service", "MainPID": str(100 + offset)}
                for offset, lane in enumerate(runner.WORKERS)]
    report = {"owner": "h3val_0123456789abcdef", "prompt_id": "11111111-1111-4111-8111-111111111111",
              "isolated_url": args.isolated_url,
              "client_id": "22222222-2222-4222-8222-222222222222", "started_at": runner.time.time(),
              "status": "prepared", "shape": shape, "prefix": prefix,
              "submit_attempted": False, "lease_released": False}
    state = {"lease": None, "calls": [], "mode": "success", "submitted": False,
             "isolated_vram": 0, "foreign": False, "history": None, "interrupts": 0}

    def item(foreign=False):
        return [0, "foreign" if foreign else report["prompt_id"], graph, {"client_id": report["client_id"]}, []]

    def transport(request):
        body = json.loads(request.content) if request.content else None
        state["calls"].append((request.method, str(request.url), body))
        if request.url.port == 8789:
            assert request.headers["authorization"] == "Bearer offline-only"
        else:
            assert "authorization" not in request.headers
        path = request.url.path
        if path == runner.CAPACITY:
            return httpx.Response(200, json={"active": [], "queues": [{"lane_id": lane, "queued_or_running": 0}
                                                                       for lane in runner.WORKERS],
                                             "validation_lease": state["lease"],
                                             "resources": {"swap_used_bytes": 0, "cgroup_swap_bytes": 0},
                                             "policy": {"resources": {"max_swap_gib": 1}}})
        if path == runner.LEASE:
            if request.method == "DELETE":
                assert report.get("isolated_unload_confirmed")
                assert report.get("reconciliation")
                state["lease"] = None
            else:
                state["lease"] = {"owner": body["owner"], "expires_at": runner.time.time() + body["ttl_seconds"]}
            return httpx.Response(200, json={"ok": True})
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.34.0"}})
        if path == "/queue" and request.method == "GET":
            running = []
            if request.url.port == int(args.isolated_url.rsplit(":", 1)[1]) and state["foreign"] and state["submitted"]:
                running = [item(True)]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if path == "/free":
            assert state["lease"] is not None
            assert body == {"unload_models": True, "free_memory": True}
            return httpx.Response(200)
        if path == "/prompt":
            assert request.url.port == int(args.isolated_url.rsplit(":", 1)[1])
            assert body["prompt_id"] == report["prompt_id"]
            assert body["client_id"] == report["client_id"]
            assert body["prompt"] == graph
            assert "h3" not in body["extra_data"]
            state["submitted"] = True
            if state["mode"] in {"400", "500", "malformed400"}:
                code = 500 if state["mode"] == "500" else 400
                return httpx.Response(code, json={"error": {"type": "prompt_outputs_failed_validation"}}
                                      if state["mode"] != "malformed400" else {"detail": "unknown"})
            if state["mode"] == "timeout":
                raise httpx.ReadTimeout("ambiguous submit", request=request)
            state["history"] = {report["prompt_id"]: {
                "prompt": item(), "status": {"completed": True, "status_str": "success", "messages": [
                    ["execution_start", {"timestamp": 1000}], ["execution_cached", {"nodes": []}],
                    ["execution_success", {"timestamp": 2000}]]}}}
            return httpx.Response(200, json={"prompt_id": report["prompt_id"]})
        if path.startswith("/history/"):
            return httpx.Response(200, json=state["history"] or {})
        pytest.fail("unexpected request: " + str(request.url))

    fleet = httpx.Client(transport=httpx.MockTransport(transport), headers={"Authorization": "Bearer offline-only"})
    comfy = httpx.Client(transport=httpx.MockTransport(transport))
    driver = runner.Driver(args, report, graph, fleet, comfy)

    def wait(seconds):
        if not driver.stop.is_set():
            clock.sleep(seconds)
        return driver.stop.is_set()

    monkeypatch.setattr(driver.stop, "wait", wait)

    def observe(baseline=False):
        if baseline:
            report["baseline"] = {"isolated": isolated, "services": services}

    def collect(history, record):
        report["execution"] = runner.verify_execution(history, report["prompt_id"], graph)
        report["status"] = "generated_pending_quality_review"

    monkeypatch.setattr(driver, "observe", observe)
    monkeypatch.setattr(driver, "collect", collect)
    monkeypatch.setattr(runner, "isolated_snapshot", lambda *args: isolated)
    monkeypatch.setattr(runner, "fast_vram", lambda pid, gpu: state["isolated_vram"] if pid == "2074565" else 0)
    yield driver, state
    fleet.close()
    comfy.close()


def writes(state, path, method="POST"):
    return [call for call in state["calls"] if call[0] == method and call[1].endswith(path)]


def test_virtual_clock_is_local_and_deadline_still_expires(runtime):
    import time

    driver, state = runtime
    assert runner.time is not time
    assert runner.time.monotonic() == 0
    driver.stop.wait(driver.args.timeout)
    assert runner.time.monotonic() == driver.deadline
    with pytest.raises(RuntimeError, match="execution deadline reached"):
        driver.guard()
    assert not state["calls"]


def test_virtual_stop_wait_returns_without_advancing_after_stop(runtime):
    driver, _ = runtime
    driver.stop.set()
    assert driver.stop.wait(100) is True
    assert runner.time.monotonic() == 0


def test_a4_success_unloads_before_release(runtime):
    driver, state = runtime
    started = runner.time.monotonic()
    assert driver.execute() == 0
    assert runner.time.monotonic() == started
    assert len(writes(state, "/prompt")) == 1
    assert len(writes(state, "/free")) == 2
    assert driver.report["execution"]["sampler_nodes"]
    assert driver.report["isolated_vram_mib_after_free"] <= 1024
    assert driver.report["lease_released"]
    free = next(index for index, call in enumerate(state["calls"]) if call[1] == driver.isolated_url + "/free")
    release = next(index for index, call in enumerate(state["calls"]) if call[0] == "DELETE")
    assert free < release
    isolated_calls = [call for call in state["calls"] if httpx.URL(call[1]).port in {18188, 18189}]
    assert isolated_calls
    assert all(call[1].startswith(driver.isolated_url + "/") for call in isolated_calls)
    assert any(call[1] == driver.isolated_url + "/system_stats" for call in isolated_calls)
    assert any("/history/" in call[1] for call in isolated_calls)


@pytest.mark.parametrize("case", ["A4_C0", "A4_C05", "A4_C1"])
def test_a4_realism_mock_execution_submits_once_and_reconciles(runtime, case):
    driver, state = runtime
    driver.args.case = case
    driver.workflow["6"]["inputs"]["prompt"] = "r34l1sm\n共同粉色广告 IR 原文"
    driver.workflow["5"]["inputs"]["lora_name"] = runner.A4_LORA if case == "A4_C0" else f"offline-fixture-{case}.safetensors"
    driver.report["shape"] = runner.validate_workflow(driver.workflow, case)
    original = copy.deepcopy(driver.workflow)
    assert driver.execute() == 0
    assert driver.workflow == original
    submissions = writes(state, "/prompt")
    assert len(submissions) == 1
    assert submissions[0][2]["extra_data"]["isolated_comparison"]["case"] == case
    assert submissions[0][2]["prompt"] == original
    assert driver.report["isolated_unload_confirmed"]
    assert driver.report["lease_released"]
    assert driver.report["execution"]["sampler_nodes"] == ["10"]


@pytest.mark.parametrize("case,namespace", [("B8", "h3-isolated"), ("D4", "h3-comparison")])
def test_candidate_mock_execution_uses_same_isolation_and_evidence(runtime, b8_graph, d4_graph, case, namespace):
    driver, state = runtime
    driver.workflow.clear()
    driver.workflow.update(b8_graph if case == "B8" else d4_graph)
    driver.args.case = case
    driver.args.run_id = "offline-candidate"
    driver.deadline = runner.time.monotonic() + 1
    driver.report["shape"] = runner.validate_workflow(driver.workflow, case)
    driver.report["prefix"] = f"video/{namespace}/offline-candidate/{case}"
    driver.workflow["13"]["inputs"]["filename_prefix"] = driver.report["prefix"]
    assert driver.execute() == 0
    submissions = writes(state, "/prompt")
    assert len(submissions) == 1
    assert submissions[0][2]["extra_data"] == {"isolated_comparison": {"run_id": "offline-candidate", "case": case}}
    assert driver.report["execution"]["sampler_nodes"] == ["10"]
    if case == "B8":
        assert driver.report["execution"]["model_composer_nodes"] == ["5"]
        assert driver.report["execution"]["execution_plan_nodes"] == ["7"]
        assert driver.report["execution"]["expected_steps"] == 8
    else:
        assert "model_composer_nodes" not in driver.report["execution"]
    assert driver.report["status"] == "generated_pending_quality_review"
    assert driver.report["isolated_unload_confirmed"]
    assert driver.report["lease_released"]
    assert len(writes(state, "/free")) == 2


def test_http400_rejected_empty_queue_reconciles(runtime):
    driver, state = runtime
    state["mode"] = "400"
    assert driver.execute() == 1
    assert driver.report["submit_rejected_http400"]
    assert "HTTP400" in driver.report["reconciliation"]
    assert driver.report["lease_released"]
    assert len(writes(state, "/prompt")) == 1
    assert not writes(state, "/interrupt")
    assert driver.report["isolated_unload_confirmed"]


@pytest.mark.parametrize("mode", ["500", "timeout", "malformed400"])
def test_ambiguous_submission_retains_lease_never_retries(runtime, mode):
    driver, state = runtime
    state["mode"] = mode
    assert driver.execute() == 1
    assert driver.report["status"] == "needs_reconciliation"
    assert not driver.report["lease_released"]
    assert len(writes(state, "/prompt")) == 1
    assert not writes(state, runner.LEASE, "DELETE")
    assert not writes(state, "/interrupt")
    assert driver.report["retained_lease"]["owner"] == driver.report["owner"]


def test_isolated_vram_above_ceiling_forbids_release(runtime):
    driver, state = runtime
    state["isolated_vram"] = 1025
    assert driver.execute() == 1
    assert driver.report["status"] == "needs_reconciliation"
    assert not writes(state, runner.LEASE, "DELETE")
    assert driver.report["isolated_vram_mib_after_free"] == 1025


def test_exact_vram_ceiling_allows_release(runtime):
    driver, state = runtime
    state["isolated_vram"] = 1024
    assert driver.execute() == 0
    assert driver.report["lease_released"]


def test_foreign_queue_forbids_cancel_free_and_release(runtime):
    driver, state = runtime
    state["foreign"] = True
    assert driver.execute() == 1
    assert driver.report["status"] == "needs_reconciliation"
    assert not writes(state, "/interrupt")
    assert not writes(state, runner.LEASE, "DELETE")
    assert all(call[1] != driver.isolated_url + "/free" for call in writes(state, "/free"))


@pytest.mark.parametrize("value", [
    "https://127.0.0.1:18189", "http://localhost:18189", "http://127.0.0.2:18189",
    "http://[::1]:18189", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:018189", "http://127.0.0.1:+18189", "http://127.0.0.1:18189/",
    "http://user:password@127.0.0.1:18189", "http://127.0.0.1:18189@evil.example",
    "http://127.0.0.1:18189?query", "http://127.0.0.1:18189#fragment", "http://127.0.0.1:18189/path",
    "http://127.0.0.1:18189\\path", "http://127.0.0.1:18189\n", " http://127.0.0.1:18189",
    "http://127.0.0.1:１８１８９", *runner.WORKERS.values(), None,
])
def test_isolated_url_rejects_noncanonical_origins_and_production(value):
    with pytest.raises(ValueError):
        runner.validate_isolated_url(value)


def test_isolated_url_default_and_cli_validation():
    defaults = runner.parser()
    assert defaults.get_default("isolated_url") == "http://127.0.0.1:18188"
    for port in (1, 18188, 18189, 65535):
        url = f"http://127.0.0.1:{port}"
        assert runner.validate_isolated_url(url) == url
    with pytest.raises(SystemExit):
        defaults.parse_args(["--isolated-url", "http://evil.example:18189"])


def test_invalid_url_fails_before_output_or_network(graph, tmp_path, monkeypatch):
    source = tmp_path / "workflow.json"
    source.write_text(json.dumps(graph))
    output = tmp_path / "not-created"
    args = SimpleNamespace(workflow=source, workflow_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                           case="A4", isolated_url="http://127.0.0.1:18189/", output_dir=output)
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="isolated URL"):
        runner.run(args)
    assert not output.exists()


@pytest.fixture
def process_tree(tmp_path):
    process = tmp_path / "123"
    for name in ("ns", "net", "fd"):
        (process / name).mkdir(parents=True)
    (process / "ns/net").symlink_to(os.readlink("/proc/self/ns/net"))
    (process / "net/tcp").write_text(
        "header\n0: 0100007F:470D 00000000:0000 0A 0 0 0 0 0 999\n"
        "1: 0100007F:470C 00000000:0000 0A 0 0 0 0 0 888\n")
    (process / "fd/3").symlink_to("socket:[999]")
    (process / "stat").write_text("123 (python worker) " + " ".join(["S"] + ["0"] * 18 + ["12345"]))
    return process


def test_listener_port_is_bound_to_pid_not_just_network_table(process_tree):
    evidence = runner.listener_evidence(process_tree, "http://127.0.0.1:18189")
    assert evidence == {"isolated_url": "http://127.0.0.1:18189", "listener_inode": "999",
                        "process_start_ticks": "12345"}
    with pytest.raises(RuntimeError, match="does not uniquely own"):
        runner.listener_evidence(process_tree, "http://127.0.0.1:18188")


@pytest.mark.parametrize("change", ["namespace", "not_listening", "wildcard", "different_pid", "replaced_listener"])
def test_listener_identity_fail_closed(process_tree, change):
    if change == "namespace":
        (process_tree / "ns/net").unlink()
        (process_tree / "ns/net").symlink_to("net:[foreign]")
    elif change == "different_pid":
        (process_tree / "fd/3").unlink()
    else:
        table = process_tree / "net/tcp"
        old, new = {"not_listening": ("0A", "01"), "wildcard": ("0100007F", "00000000"),
                    "replaced_listener": ("999", "777")}[change]
        table.write_text(table.read_text().replace(old, new))
    with pytest.raises(RuntimeError):
        runner.listener_evidence(process_tree, "http://127.0.0.1:18189")


def test_snapshot_keeps_uuid_and_cgroup_checks_with_url_binding(process_tree, monkeypatch):
    unit = "isolated-d4.service"
    gpu = "GPU-11111111-1111-1111-1111-111111111111"
    cgroup = runner.CGROUP + "/" + unit
    (process_tree / "cgroup").write_text("0::" + cgroup + "\n")
    (process_tree / "environ").write_bytes(b"CUDA_VISIBLE_DEVICES=" + gpu.encode() + b"\0")
    monkeypatch.setattr(runner, "Path", lambda value: process_tree.parent if value == "/proc" else Path(value))
    def command(*args):
        if args[0] == "systemctl":
            return f"Id={unit}\nActiveState=active\nMainPID=123\nNRestarts=0\nSlice=h3-compute.slice\nControlGroup={cgroup}"
        assert args[0] == "nvidia-smi"
        return gpu + ", NVIDIA GeForce RTX 4060 Ti"
    monkeypatch.setattr(runner, "command", command)
    state = runner.isolated_snapshot(unit, gpu, "http://127.0.0.1:18189")
    assert state["isolated_url"] == "http://127.0.0.1:18189"
    assert state["gpu_uuid"] == gpu
    with pytest.raises(RuntimeError, match="does not uniquely own"):
        runner.isolated_snapshot(unit, gpu, "http://127.0.0.1:18188")
    (process_tree / "environ").write_bytes(b"CUDA_VISIBLE_DEVICES=0\0")
    with pytest.raises(RuntimeError, match="full GPU UUID"):
        runner.isolated_snapshot(unit, gpu, "http://127.0.0.1:18189")
    (process_tree / "cgroup").write_text("0::/wrong-slice\n")
    with pytest.raises(RuntimeError, match="cgroup does not match"):
        runner.isolated_snapshot(unit, gpu, "http://127.0.0.1:18189")


def test_driver_rejects_url_change_from_frozen_report(runtime):
    driver, _ = runtime
    driver.args.isolated_url = "http://127.0.0.1:18190"
    with pytest.raises(ValueError, match="frozen report"):
        runner.Driver(driver.args, driver.report, driver.workflow, driver.fleet, driver.comfy)


@pytest.mark.parametrize("queue_key,path", [("queue_running", "/interrupt"), ("queue_pending", "/queue")])
def test_targeted_cancellation_uses_instance_url(runtime, monkeypatch, queue_key, path):
    driver, _ = runtime
    driver.observe(baseline=True)
    driver.report["submit_attempted"] = True
    driver.args.cancel_timeout = 1
    calls = []
    monkeypatch.setattr(driver, "capacity", lambda: None)
    queue = {"queue_running": [], "queue_pending": []}
    queue[queue_key] = [[0, driver.report["prompt_id"], driver.workflow, {"client_id": driver.report["client_id"]}]]
    monkeypatch.setattr(driver, "queues", lambda **kwargs: queue if not calls else {"queue_running": [], "queue_pending": []})
    monkeypatch.setattr(driver, "history", lambda: ({}, {"status": {"status_str": "success"}} if calls else None))
    monkeypatch.setattr(driver, "request", lambda *args: calls.append(args))
    assert driver.reconcile() == "owned terminal history and empty queues"
    body = {"prompt_id": driver.report["prompt_id"]} if path == "/interrupt" else {"delete": [driver.report["prompt_id"]]}
    assert calls == [("POST", driver.isolated_url + path, body)]


def test_listener_drift_retains_lease_and_refuses_free(runtime, monkeypatch):
    driver, state = runtime
    monkeypatch.setattr(runner, "isolated_snapshot", lambda *args: {"MainPID": "other-worker"})
    assert driver.execute() == 1
    assert driver.report["status"] == "needs_reconciliation"
    assert not writes(state, runner.LEASE, "DELETE")
    assert not any(call[1] == driver.isolated_url + "/free" for call in writes(state, "/free"))


def test_media_download_uses_instance_url(runtime, monkeypatch):
    driver, _ = runtime
    driver.deadline = runner.time.monotonic() + 5
    prefix = Path(driver.report["prefix"])
    artifact = {"filename": prefix.name + "_00001.mp4", "subfolder": str(prefix.parent), "type": "output"}
    record = {"outputs": {driver.report["shape"]["save_node"]: {"images": [artifact]}}}
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=b"offline-media-stub")
    monkeypatch.setattr(driver, "guard", lambda: None)
    monkeypatch.setattr(runner, "verify_execution", lambda *args: {"offline_test": True})
    monkeypatch.setattr(runner, "command", lambda *args: json.dumps({"streams": [{"codec_type": "video", "nb_read_frames": "362"}]}))
    monkeypatch.setattr(runner, "validate_media", lambda *args, **kwargs: {"ok": True})
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        monkeypatch.setattr(driver, "comfy", client)
        runner.Driver.collect(driver, {}, record)
    assert len(requests) == 1
    assert str(requests[0].url).split("?", 1)[0] == driver.isolated_url + "/view"
    assert dict(requests[0].url.params) == artifact
    assert (driver.args.output_dir / "video.mp4").read_bytes() == b"offline-media-stub"


@pytest.mark.parametrize("budget,valid", [(0.1, True), (180, True), (1800, True),
                                         (0, False), (-1, False), (1800.1, False),
                                         (float("nan"), False), (float("inf"), False)])
def test_idle_wait_timeout_boundaries_are_offline(graph, monkeypatch, budget, valid):
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("no network during preparation"))
    assert runner.parser().get_default("idle_wait_timeout") == 180
    with tempfile.TemporaryDirectory(prefix="idle-budget-", dir=SCRIPTS.parent / "tests") as directory:
        root = Path(directory)
        source = root / "workflow.json"
        source.write_text(json.dumps(graph))
        output = root / "run"
        args = runner.parser().parse_args([
            "--workflow", str(source), "--workflow-sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
            "--case", "A4", "--run-id", "offline-wait", "--output-dir", str(output),
            "--isolated-unit", "offline-wait.service", "--idle-wait-timeout", str(budget),
            "--isolated-gpu-uuid", "GPU-11111111-1111-1111-1111-111111111111"])
        if valid:
            assert runner.run(args) == 0
            assert json.loads((output / "report.json").read_bytes())["idle_wait_timeout"] == budget
        else:
            with pytest.raises(ValueError, match="idle wait timeout"):
                runner.run(args)
            assert not output.exists()


@pytest.mark.parametrize("budget,ready_at,succeeds,elapsed", [
    (180, 210, False, 180), (1800, 210, True, 210), (1800, 9999, False, 1800),
])
def test_idle_wait_budget_changes_only_wait_deadline(runtime, monkeypatch, budget, ready_at, succeeds, elapsed):
    driver, state = runtime
    driver.args.idle_wait_timeout = budget
    driver.args.poll_interval = 30
    policy = {"max_swap_gib": 1, "swap_hard_limit_gib": 8,
              "swap_recovery_mode": "stable_idle_exclusive_fast_only", "swap_idle_stable_seconds": 60}
    driver.report["resource_limits"] = copy.deepcopy(policy)
    clock = [0]
    def capacity(**kwargs):
        return {"resources": {"swap_used_bytes": 3 * 1024**3, "cgroup_swap_bytes": 2 * 1024**3,
                              "timestamp": clock[0], "swap_recovery": {
                                  "ready": clock[0] >= ready_at, "exclusive_single": True,
                                  "stable_seconds_observed": 60}}}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(driver.stop, "wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(driver, "capacity", capacity)
    monkeypatch.setattr(driver, "queues", lambda **kwargs: {})
    monkeypatch.setattr(driver, "save", lambda: None)
    if succeeds:
        driver.await_idle_swap(capacity())
        assert driver.report["swap_recovery_baseline"]["host_bytes"] == 3 * 1024**3
    else:
        with pytest.raises(RuntimeError, match=f"within {budget} seconds; no submission"):
            driver.await_idle_swap(capacity())
        assert "swap_recovery_baseline" not in driver.report
    assert clock[0] == elapsed
    assert driver.report["resource_limits"] == policy
    assert not driver.report["submit_attempted"]
    assert not state["calls"]


@pytest.mark.parametrize("override", [
    {"stable_seconds_observed": 59}, {"exclusive_single": False}, {"ready": False},
])
def test_long_idle_wait_does_not_relax_stability_requirements(runtime, monkeypatch, override):
    driver, _ = runtime
    driver.args.idle_wait_timeout = 1800
    driver.report["resource_limits"] = {
        "max_swap_gib": 1, "swap_hard_limit_gib": 8, "swap_idle_stable_seconds": 60,
        "swap_recovery_mode": "stable_idle_exclusive_fast_only"}
    snapshot = {"swap_used_bytes": 3 * 1024**3, "cgroup_swap_bytes": 2 * 1024**3,
                "timestamp": 1, "swap_recovery": {
                    "ready": True, "exclusive_single": True, "stable_seconds_observed": 60, **override}}
    times = iter([0, 1800])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(driver, "save", lambda: None)
    with pytest.raises(RuntimeError, match="no stable idle swap window"):
        driver.await_idle_swap({"resources": snapshot})
    assert "swap_recovery_baseline" not in driver.report


def test_long_idle_wait_keeps_hard_memory_ceiling(runtime):
    driver, _ = runtime
    driver.args.idle_wait_timeout = 1800
    driver.report["resource_limits"] = {
        "max_swap_gib": 1, "swap_hard_limit_gib": 8, "swap_idle_stable_seconds": 60,
        "swap_recovery_mode": "stable_idle_exclusive_fast_only"}
    with pytest.raises(RuntimeError, match="hard ceiling crossed"):
        driver.await_idle_swap({"resources": {"swap_used_bytes": 8 * 1024**3 + 1, "cgroup_swap_bytes": 0}})
