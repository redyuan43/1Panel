import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measured_peak_history.py"
sys.path.insert(0, str(SCRIPT.parent))
import measured_peak_history as peak


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    graph = {"prompt": "r34l1sm unchanged IR", "weights": {"A4": 1, "People": 0.5}}
    identity = {"Id": "isolated.service", "MainPID": "123", "process_start_ticks": "456",
                "gpu_uuid": "GPU-test", "isolated_url": "http://127.0.0.1:18188",
                "ControlGroup": "/h3.slice/h3-compute.slice/isolated.service"}
    binding = {"case": "A4_C05", "workflow_sha256": digest(encoded(graph)),
               "validator_sha256": "a" * 64, "gpu_uuid": "GPU-test", "unit": "isolated.service",
               "endpoint": "http://127.0.0.1:18188", "runtime_profile_id": "reviewed-boot-pid-123",
               "shape": {"width": 480, "height": 864, "length": 362, "fps": 24, "steps": 4}}
    baseline = {"timestamp": 100, "boot_id": "boot1", "isolated": identity,
                "cgroup_current_bytes": 40 * peak.GIB, "kernel_alerts": [],
                "cgroup_events": {"oom": 0, "oom_kill": 0}}
    metrics = [dict(baseline, timestamp=timestamp, cgroup_current_bytes=current * peak.GIB)
               for timestamp, current in [(110, 43), (120, 46), (130, 44), (140, 40)]]
    report = {"case": "A4_C05", "run_id": "run1", "prompt_id": "prompt1",
              "status": "generated_pending_quality_review", "baseline": baseline,
              "lease_released": True, "isolated_unload_confirmed": True,
              "isolated_vram_mib_after_free": 236, "reconciliation": "owned terminal history and empty queues",
              "input_workflow_sha256": binding["workflow_sha256"], "isolated_gpu_uuid": "GPU-test",
              "artifact_sha256": digest(b"fixture-media-not-a-real-video"),
              "media": {"ok": True, "audio_codec": "aac", "width": 480, "height": 864,
                        "frame_count": 362, "fps": 24, "duration_seconds": 362 / 24},
              "submitted_at": 105, "finished_at": 142,
              "execution": {"started_at": 106, "finished_at": 135, "execution_seconds": 29,
                            "sampler_nodes": ["10"], "cached_nodes": []}}
    descriptor = {"schema_version": 1, "case": "A4_C05", "run_id": "run1", "prompt_id": "prompt1",
                  "profile_key": digest(encoded(binding)), "profile_binding": binding,
                  "worker_count": 1, "scope": "single_worker_aggregate",
                  "identity": {"boot_id": "boot1", "isolated": identity, "cgroup_events": baseline["cgroup_events"]}}
    return {"directory": tmp_path, "descriptor": descriptor, "report": report, "baseline": baseline,
            "metrics": metrics, "workflow": graph, "media": b"fixture-media-not-a-real-video"}


def write_bundle(bundle):
    files = {}
    for name in ("report", "baseline", "metrics", "workflow", "media"):
        value = bundle[name]
        if name == "metrics":
            raw = b"\n".join(encoded(sample) for sample in value) + b"\n"
        else:
            raw = value if name == "media" else encoded(value)
        path = bundle["directory"] / name
        path.write_bytes(raw)
        files[name] = {"path": str(path), "sha256": digest(raw)}
    bundle["descriptor"]["files"] = files
    return bundle["descriptor"]


def resolve(bundle, history, **kwargs):
    descriptor = bundle["descriptor"]
    return peak.resolve_budget(descriptor["case"], 18 * peak.GIB, history, descriptor["profile_key"], **kwargs)


def test_single_aggregate_measured_delta_and_provenance(bundle):
    history = peak.collect_peak_run(write_bundle(bundle))
    result = resolve(bundle, history)
    assert not result["fallback"]
    assert result["peak_delta_bytes"] == 6 * peak.GIB
    assert result["candidate_budget_bytes"] == result["budget_bytes"] == 8 * peak.GIB
    assert result["provenance"][0]["scope"] == "single_worker_aggregate"
    assert result["experimental"] and not result["hardware_certified"]


@pytest.mark.parametrize("margin,ratio,expected", [(0, 0, 8), (5, 0.1, 11), (2, 0.5, 9)])
def test_explicit_margins_never_remove_minimum(bundle, margin, ratio, expected):
    result = resolve(bundle, peak.collect_peak_run(write_bundle(bundle)),
                     margin_bytes=margin * peak.GIB, margin_ratio=ratio)
    assert result["candidate_budget_bytes"] == expected * peak.GIB


def test_ten_percent_minimum_even_with_lower_requested_ratio(bundle):
    bundle["metrics"][1]["cgroup_current_bytes"] = 70 * peak.GIB
    result = resolve(bundle, peak.collect_peak_run(write_bundle(bundle)), margin_ratio=0)
    assert result["candidate_budget_bytes"] == 33 * peak.GIB


@pytest.mark.parametrize("field,value", [("case", "C1"), ("run_id", "other"), ("prompt_id", "other"),
    ("status", "running"), ("lease_released", False), ("isolated_unload_confirmed", False),
    ("isolated_vram_mib_after_free", 1025), ("reconciliation", ""), ("artifact_sha256", "b" * 64),
    ("finished_at", 130), ("submitted_at", 121), ("isolated_gpu_uuid", "GPU-other"),
    ("input_workflow_sha256", "b" * 64), ("error", "failed")])
def test_reject_report_mismatches(bundle, field, value):
    bundle["report"][field] = value
    with pytest.raises(ValueError):
        peak.collect_peak_run(write_bundle(bundle))


@pytest.mark.parametrize("mutation", ["duplicate", "gap", "reverse", "nan", "negative", "boot", "pid", "oom", "kernel", "missing", "truncated"])
def test_invalid_metrics_rejected(bundle, mutation):
    sample = bundle["metrics"][1]
    if mutation == "duplicate":
        sample["timestamp"] = 110
    elif mutation == "gap":
        sample["timestamp"] = 129
    elif mutation == "reverse":
        sample["timestamp"] = 109
    elif mutation == "nan":
        sample["timestamp"] = float("nan")
    elif mutation == "negative":
        sample["cgroup_current_bytes"] = -1
    elif mutation == "boot":
        sample["boot_id"] = "other"
    elif mutation == "pid":
        sample["isolated"] = dict(sample["isolated"], MainPID="999")
    elif mutation == "oom":
        sample["cgroup_events"] = {"oom": 1, "oom_kill": 0}
    elif mutation == "kernel":
        sample["kernel_alerts"] = ["Xid"]
    elif mutation == "missing":
        del sample["cgroup_current_bytes"]
    elif mutation == "truncated":
        bundle["metrics"] = bundle["metrics"][:2]
    with pytest.raises(ValueError):
        peak.collect_peak_run(write_bundle(bundle))


@pytest.mark.parametrize("name", ["report", "baseline", "metrics", "workflow", "media"])
def test_source_hash_tampering_rejected(bundle, name):
    descriptor = write_bundle(bundle)
    Path(descriptor["files"][name]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        peak.collect_peak_run(descriptor)


def test_trigger_change_cannot_reuse_profile(bundle):
    bundle["workflow"]["prompt"] = "new trigger"
    with pytest.raises(ValueError, match="profile workflow mismatch"):
        peak.collect_peak_run(write_bundle(bundle))


def test_cached_sampler_and_short_media_rejected(bundle):
    bundle["report"]["execution"]["cached_nodes"] = ["10"]
    with pytest.raises(ValueError, match="cached"):
        peak.collect_peak_run(write_bundle(bundle))
    bundle["report"]["execution"]["cached_nodes"] = []
    bundle["report"]["media"]["frame_count"] = 120
    with pytest.raises(ValueError, match="shape"):
        peak.collect_peak_run(write_bundle(bundle))


def test_missing_or_raw_claimed_verified_history_falls_back(bundle):
    for history in (None, [], {"verified": True, "peak_delta_bytes": 1}):
        result = resolve(bundle, history)
        assert result["fallback"] and result["candidate_budget_bytes"] == 18 * peak.GIB
        assert result["reason"] == "history_not_verified"


def test_case_and_profile_are_exact_no_aliases(bundle):
    history = peak.collect_peak_run(write_bundle(bundle))
    for case, key in [("C1", bundle["descriptor"]["profile_key"]), ("A4_C05", "b" * 64)]:
        result = peak.resolve_budget(case, 18 * peak.GIB, history, key)
        assert result["fallback"] and result["candidate_budget_bytes"] == 18 * peak.GIB


def test_floor_survives_reload_lower_margin_and_missing_sources(bundle):
    path = bundle["directory"] / "history.json"
    history = peak.collect_peak_run(write_bundle(bundle), margin_bytes=20 * peak.GIB)
    peak.save_history(path, history)
    result = resolve(bundle, peak.load_history(path))
    assert result["candidate_budget_bytes"] == 26 * peak.GIB
    peak.save_history(path, peak.collect_peak_run(bundle["descriptor"]))
    assert resolve(bundle, peak.load_history(path))["candidate_budget_bytes"] == 26 * peak.GIB
    (bundle["directory"] / "metrics").unlink()
    result = resolve(bundle, peak.load_history(path))
    assert result["fallback"] and result["candidate_budget_bytes"] == 26 * peak.GIB
    assert "source_verification_failed" in result["reason"]


def test_multiworker_aggregate_never_assigned_to_case(bundle):
    bundle["descriptor"]["worker_count"] = 2
    with pytest.raises(ValueError):
        peak.collect_peak_run(write_bundle(bundle))


def test_perworker_scope_uses_only_subtree_delta(bundle):
    make_batch(bundle)
    result = resolve(bundle, peak.collect_peak_run(write_bundle(bundle)))
    assert result["peak_delta_bytes"] == 2 * peak.GIB
    assert result["candidate_budget_bytes"] == 4 * peak.GIB
    assert result["provenance"][0]["scope"] == "perworker_subtree"
    bundle["metrics"][1]["perworker_subtree"]["A4_C05"]["prompt_id"] = "foreign"
    with pytest.raises(ValueError, match="prompt"):
        peak.collect_peak_run(write_bundle(bundle))


def make_batch(bundle):
    from admission_reconciliation import start_record, observe, finish

    descriptor = bundle["descriptor"]
    descriptor.update(worker_count=2, scope="perworker_subtree")
    descriptor["identity"]["isolated"]["start_ticks"] = "456"
    for index, sample in enumerate([bundle["baseline"], *bundle["metrics"]]):
        sample.update(ok=True, memory_available_bytes=64 * peak.GIB,
                      worker_memory_bytes={"A4_C05": (2 + min(index, 2)) * peak.GIB})
        sample["perworker_subtree"] = {"A4_C05": {"case": "A4_C05", "prompt_id": "prompt1",
            "identity": descriptor["identity"]["isolated"], "memory_current_bytes": (2 + min(index, 2)) * peak.GIB}}
    task = copy.deepcopy(bundle["report"])
    accounting = start_record({"projected_cgroup_bytes": 50 * peak.GIB}, bundle["baseline"], "A4_C05", {"A4_C05": 2 * peak.GIB})
    for sample in bundle["metrics"]:
        observe(accounting, sample)
    task.update(workflow_sha256=task["input_workflow_sha256"], gpu_uuid=task["isolated_gpu_uuid"],
                admission_baseline=copy.deepcopy(bundle["baseline"]), resource_reconciliation=finish(accounting),
                profile_key=descriptor["profile_key"], profile_binding=descriptor["profile_binding"])
    bundle["report"].update(status="generated_pending_quality_review_drained", both_unloaded=True,
                            tasks=[task, {"case": "D4"}])


def test_batch_report_builder_and_reconciliation_integration(bundle):
    make_batch(bundle)
    bundle["report"]["baseline"] = dict(bundle["baseline"], timestamp=10, cgroup_current_bytes=peak.GIB)
    earlier = dict(bundle["baseline"], timestamp=20, cgroup_current_bytes=99 * peak.GIB)
    later = dict(bundle["baseline"], timestamp=200, cgroup_current_bytes=99 * peak.GIB)
    bundle["metrics"] = [{"sample": sample, "identities": {"workers": {"A4_C05": sample["isolated"]}}}
                         for sample in [earlier, bundle["baseline"], *bundle["metrics"], later]]
    source = write_bundle(bundle)
    descriptor = peak.build_source_manifest(source["files"]["report"]["path"], case="A4_C05",
        metrics_path=source["files"]["metrics"]["path"], workflow_path=source["files"]["workflow"]["path"],
        media_path=source["files"]["media"]["path"])
    result = resolve(bundle, peak.collect_peak_run(descriptor))
    assert not result["fallback"] and result["peak_delta_bytes"] == 2 * peak.GIB
    assert result["provenance"][0]["baseline_timestamp"] == 100
    archive = bundle["directory"] / "auto-history.json"
    command = [sys.executable, "-B", str(SCRIPT), "collect", "--from-report", descriptor["files"]["report"]["path"],
               "--case", "A4_C05", "--metrics", descriptor["files"]["metrics"]["path"],
               "--workflow", descriptor["files"]["workflow"]["path"], "--media", descriptor["files"]["media"]["path"],
               "--history", str(archive)]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert resolve(bundle, peak.load_history(archive))["candidate_budget_bytes"] == 4 * peak.GIB


def test_successful_task_budget_is_inherited_as_validated_floor(bundle):
    make_batch(bundle)
    task = bundle["report"]["tasks"][0]
    task["peak_budget"] = {"candidate_budget_bytes": 24 * peak.GIB, "profile_key": task["profile_key"]}
    history = peak.collect_peak_run(write_bundle(bundle))
    result = resolve(bundle, history)
    assert result["peak_delta_bytes"] == 2 * peak.GIB
    assert result["candidate_budget_bytes"] == 24 * peak.GIB
    assert result["provenance"][0]["successful_budget_floor_bytes"] == 24 * peak.GIB
    archive = bundle["directory"] / "budget-floor-history.json"
    peak.save_history(archive, history)
    assert resolve(bundle, peak.load_history(archive))["candidate_budget_bytes"] == 24 * peak.GIB
    task["peak_budget"]["profile_key"] = "b" * 64
    with pytest.raises(ValueError, match="successful budget profile mismatch"):
        peak.collect_peak_run(write_bundle(bundle))


@pytest.mark.parametrize("post_delta,accounting_delta", [(9, 2), (2, 11), (9, 11)])
def test_boundary_and_own_accounting_peaks_only_raise_budget(bundle, post_delta, accounting_delta):
    make_batch(bundle)
    bundle["metrics"][-1]["perworker_subtree"]["A4_C05"]["memory_current_bytes"] = (2 + post_delta) * peak.GIB
    accounting = bundle["report"]["tasks"][0]["resource_reconciliation"]
    accounting["workers"]["A4_C05"].update(peak_delta_bytes=accounting_delta * peak.GIB,
                                           peak_bytes=(2 + accounting_delta) * peak.GIB)
    accounting["aggregate_cgroup"]["peak_delta_bytes"] = 100 * peak.GIB
    accounting["workers"]["D4"] = {"peak_delta_bytes": 100 * peak.GIB}
    history = peak.collect_peak_run(write_bundle(bundle))
    result = resolve(bundle, history)
    assert result["peak_delta_bytes"] == 2 * peak.GIB
    assert result["budget_peak_delta_bytes"] == max(post_delta, accounting_delta) * peak.GIB
    assert result["candidate_budget_bytes"] == (max(post_delta, accounting_delta) + 2) * peak.GIB
    evidence = result["provenance"][0]["peak_evidence"]
    assert evidence["during_execution"]["finished_at"] == 135
    assert evidence["first_postcompletion"]["timestamp"] == 140
    assert evidence["worker_accounting"]["case"] == "A4_C05"
    assert evidence["worker_accounting"]["scope"] == "perworker_subtree"
    archive = bundle["directory"] / "boundary-history.json"
    peak.save_history(archive, history)
    assert resolve(bundle, peak.load_history(archive))["candidate_budget_bytes"] == result["candidate_budget_bytes"]


@pytest.mark.parametrize("failure", ["observation_error", "missing_task_baseline", "conflicting_baselines", "missing_subtree", "different_profile"])
def test_batch_incomplete_evidence_cannot_reduce_budget(bundle, failure):
    make_batch(bundle)
    task = bundle["report"]["tasks"][0]
    if failure == "observation_error":
        task["resource_reconciliation"]["observation_error"] = "counter drift"
    elif failure == "missing_task_baseline":
        del task["admission_baseline"]
    elif failure == "conflicting_baselines":
        task["resource_reconciliation"]["baseline_sample"] = dict(task["admission_baseline"], timestamp=10)
    elif failure == "missing_subtree":
        del bundle["metrics"][0]["perworker_subtree"]
    else:
        task["profile_key"] = "b" * 64
    with pytest.raises(ValueError):
        peak.collect_peak_run(write_bundle(bundle))


@pytest.mark.parametrize("static", [1, 4 * peak.GIB, 18 * peak.GIB, 32 * peak.GIB])
def test_fallback_preserves_manifest_budget_exactly(bundle, static):
    result = peak.resolve_budget("A4_C05", static, None, bundle["descriptor"]["profile_key"])
    assert result["fallback"] and result["candidate_budget_bytes"] == static


@pytest.mark.parametrize("static", [0, -1, True, 4.5])
def test_invalid_static_budget_rejected(bundle, static):
    with pytest.raises(ValueError):
        peak.resolve_budget("A4_C05", static, None, bundle["descriptor"]["profile_key"])


def test_cli_collect_and_resolve_local_only(bundle):
    descriptor = write_bundle(bundle)
    manifest = bundle["directory"] / "source.json"
    manifest.write_bytes(encoded(descriptor))
    history = bundle["directory"] / "history.json"
    profile = bundle["directory"] / "profile.json"
    profile.write_bytes(encoded(descriptor["profile_key"]))
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "collect", "--from-local-files",
        str(manifest), "--history", str(history)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "resolve", "--history", str(history),
        "--case", "A4_C05", "--profile-key", str(profile)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate_budget_bytes"] == 8 * peak.GIB


def test_resolver_is_pure_after_collection(bundle, monkeypatch):
    history = peak.collect_peak_run(write_bundle(bundle))
    def forbidden(*args, **kwargs):
        raise AssertionError("resolver must not access files")
    monkeypatch.setattr(Path, "open", forbidden)
    assert not resolve(bundle, history)["fallback"]
