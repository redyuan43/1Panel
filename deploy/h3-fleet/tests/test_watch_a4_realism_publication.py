import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import watch_a4_realism_publication as watcher


def report(case, folder):
    return {"case": case, "status": watcher.SUCCESS, "lease_released": True,
            "isolated_unload_confirmed": True,
            "artifact": f"{watcher.REMOTE_ROOT}/{folder}/video.mp4"}


@pytest.mark.parametrize("prefix", ["../escape", "bad;cmd", "bad/name", " space"])
def test_mapping_rejects_unsafe_prefix(prefix):
    with pytest.raises(ValueError):
        watcher.folder_mapping(prefix)


def test_only_exact_three_folders_are_selected():
    assert watcher.folder_mapping() == {case: case for case in watcher.CASES}
    mapping = watcher.folder_mapping("frozen")
    assert mapping == {case: f"frozen-{case}" for case in watcher.CASES}
    assert watcher.folder_mapping(entries=[f"{case}={folder}" for case, folder in mapping.items()]) == mapping
    for entries in (["D4=unrelated"], ["A4_C05=same"] * 3,
                    [f"{case}=same" for case in watcher.CASES]):
        with pytest.raises(ValueError):
            watcher.folder_mapping(entries=entries)


@pytest.mark.parametrize("field", ["lease_released", "isolated_unload_confirmed"])
def test_cleanup_must_be_true_before_transfer(field):
    candidate = report("A4_C05", "run")
    for value in (False, None, "true", 1):
        candidate[field] = value
        assert not watcher.ready(candidate, "A4_C05", "run")


def test_missing_running_failure_and_artifact_boundaries():
    assert not watcher.ready(None, "A4_C05", "run")
    candidate = report("A4_C05", "run")
    candidate["status"] = "running"
    assert not watcher.ready(candidate, "A4_C05", "run")
    for status in ("failed", "needs_reconciliation", "unknown"):
        candidate["status"] = status
        with pytest.raises(RuntimeError):
            watcher.ready(candidate, "A4_C05", "run")
    candidate = report("A4_C05", "outside")
    with pytest.raises(ValueError, match="artifact"):
        watcher.ready(candidate, "A4_C05", "run")
    with pytest.raises(ValueError, match="case mismatch"):
        watcher.ready(candidate, "A4_C1", "outside")


def test_poll_reads_only_mapped_reports(monkeypatch):
    mapping = watcher.folder_mapping("frozen")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps(dict.fromkeys(mapping)))
    monkeypatch.setattr(watcher.subprocess, "run", run)
    assert watcher.poll(mapping, watcher.time.monotonic() + 60) == dict.fromkeys(mapping)
    command, arguments = calls[0]
    assert command[0] == "ssh" and "ivan" in command
    assert "glob" not in arguments["input"]
    assert "urllib" not in arguments["input"] and "subprocess" not in arguments["input"]
    assert all(folder in command[-1] for folder in mapping.values())


def test_collect_uses_scp_and_detects_changed_report(tmp_path, monkeypatch):
    candidate = report("A4_C05", "frozen")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_text(json.dumps(candidate) if command[-1].endswith(".json") else "video")
    monkeypatch.setattr(watcher.subprocess, "run", run)
    paths = watcher.collect("A4_C05", "frozen", candidate, tmp_path, watcher.time.monotonic() + 60)
    assert all(path.is_file() for path in paths)
    assert all(command[0] == "scp" for command in calls)
    calls.clear()
    with pytest.raises(ValueError, match="changed"):
        watcher.collect("A4_C05", "frozen", {**candidate, "prompt_id": "different"}, tmp_path, watcher.time.monotonic() + 60)
    assert len(calls) == 1


def test_publish_each_completion_without_waiting_for_all(tmp_path, monkeypatch):
    mapping = watcher.folder_mapping("run")
    destination = tmp_path / "comparison-results"
    destination.mkdir()
    (destination / "index.json").write_text('{"cases": []}')
    rounds = []
    published = []
    def poll(pending, deadline):
        rounds.append(list(pending))
        selected = next(iter(pending))
        return {case: report(case, mapping[case]) if case == selected else None for case in pending}
    monkeypatch.setattr(watcher, "poll", poll)
    monkeypatch.setattr(watcher, "collect", lambda case, *args: (Path(case), Path("video")))
    def publish(path, video, target):
        published.append(str(path))
        assert len(rounds) == len(published)
        return {"review_status": "pending_human_review"}
    monkeypatch.setattr(watcher.publisher, "publish", publish)
    sleeps = []
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)
    assert watcher.watch(mapping, tmp_path / "local", destination) == list(watcher.CASES)
    assert sleeps == [15, 15]
    assert rounds == [list(watcher.CASES), ["A4_C1", "A4_C0"], ["A4_C0"]]


@pytest.mark.parametrize("error", [ValueError("SHA mismatch"), RuntimeError("decode failed"),
                                  watcher.subprocess.CalledProcessError(1, ["scp"], stderr="transfer failed")])
def test_failures_recorded_and_propagated(tmp_path, monkeypatch, error):
    destination = tmp_path / "gallery"
    destination.mkdir()
    (destination / "index.json").write_text("{}")
    def fail(*args):
        raise error
    monkeypatch.setattr(watcher, "poll", fail)
    with pytest.raises(type(error)):
        watcher.watch(watcher.folder_mapping("run"), tmp_path / "local", destination)
    events = [json.loads(line) for line in (tmp_path / "local/watch-a4-realism-publication.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "failed" and str(error) in events[-1]["error"]


def test_not_generated_exits_at_deadline(tmp_path, monkeypatch):
    destination = tmp_path / "gallery"
    destination.mkdir()
    (destination / "index.json").write_text("{}")
    clock = [0]
    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def poll(mapping, deadline):
        watcher.remaining(deadline, 30)
        return dict.fromkeys(mapping)
    monkeypatch.setattr(watcher, "poll", poll)
    with pytest.raises(TimeoutError):
        watcher.watch(watcher.folder_mapping("run"), tmp_path / "local", destination, timeout=16)
    assert clock[0] == 16


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_rejects_unbounded_timeout(value):
    with pytest.raises(watcher.argparse.ArgumentTypeError):
        watcher.positive_timeout(value)


def test_publisher_error_stops_further_publication(tmp_path, monkeypatch):
    destination = tmp_path / "gallery"
    destination.mkdir()
    (destination / "index.json").write_text("{}")
    mapping = watcher.folder_mapping()
    monkeypatch.setattr(watcher, "poll", lambda *args: {case: report(case, case) for case in mapping})
    collected = []
    def collect(case, *args):
        collected.append(case)
        return Path("report.json"), Path("video.mp4")
    monkeypatch.setattr(watcher, "collect", collect)
    def publish(*args):
        raise ValueError("refusing to replace an already published different video")
    monkeypatch.setattr(watcher.publisher, "publish", publish)
    with pytest.raises(ValueError, match="refusing to replace"):
        watcher.watch(mapping, tmp_path / "local", destination)
    assert collected == ["A4_C05"]
    events = (tmp_path / "local/watch-a4-realism-publication.jsonl").read_text()
    assert '"event": "failed"' in events


def test_cli_defaults_and_nonzero_failure(monkeypatch):
    observed = []
    def fail(mapping, local_root, destination, timeout):
        observed.append((mapping, timeout))
        raise RuntimeError("remote failed")
    monkeypatch.setattr(watcher, "watch", fail)
    assert watcher.main(["--local-root", "/home/ai/watcher-test-not-created",
                         "--destination", "/home/ai/frontend/comparison-results"]) == 1
    assert observed == [({case: case for case in watcher.CASES}, 14400)]
