import importlib.util
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location("comparison_download", Path(__file__).parents[1] / "scripts/download_comparison_models.py")
DOWNLOAD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOWNLOAD)


@pytest.mark.parametrize("name", ["../weights", "/weights", "path/../../weights", "path\\weights", ""])
def test_reject_unsafe_names(name):
    with pytest.raises(ValueError):
        DOWNLOAD.safe_name(name)


def test_manifest_excludes_unrelated_weights():
    entries = [{"rfilename": "chosen.safetensors", "size": 12, "lfs": {"sha256": "a" * 64}},
               {"rfilename": "huge.safetensors", "size": 999999},
               {"rfilename": "README.md", "size": 8}]
    records = DOWNLOAD.selected_files("a_lightx2v", entries, ["chosen.safetensors"])
    assert {record["filename"] for record in records} == {"chosen.safetensors", "README.md"}


def test_required_weights_must_have_hashes():
    with pytest.raises(ValueError):
        DOWNLOAD.selected_files("a_lightx2v", [{"rfilename": "weights", "size": 12}], ["weights"])


def test_required_weights_must_exist():
    with pytest.raises(ValueError):
        DOWNLOAD.selected_files("a_lightx2v", [], ["weights"])


def test_cached_manifest_must_match_pinned_revisions(tmp_path):
    records = []
    for group, repo, revision, names in DOWNLOAD.SPECS:
        for name in names:
            records.append({"group": group, "repo": repo, "revision": revision,
                            "filename": name, "bytes": 12, "expected_sha256": "a" * 64})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"files": records}))
    assert len(DOWNLOAD.cached_manifest(path)) == len(records)
    records[0]["revision"] = "wrong"
    path.write_text(json.dumps({"files": records}))
    with pytest.raises(ValueError, match="revision"):
        DOWNLOAD.cached_manifest(path)


@pytest.fixture
def mirror_audit():
    path = Path(__file__).parents[1] / "experiments/optimization-20260909/a_lightx2v/mirror-audit.json"
    return json.loads(path.read_text())


def audit_records(audit):
    return [{"group": entry["group"], "repo": entry["planned_huggingface"]["repo"],
             "revision": entry["planned_huggingface"]["revision"],
             "filename": entry["planned_huggingface"]["filename"],
             "bytes": entry["planned_huggingface"]["size_bytes"],
             "expected_sha256": entry["planned_huggingface"]["sha256"]}
            for entry in audit["records"]]


def apply_audit(tmp_path, audit, records):
    path = tmp_path / "mirror.json"
    path.write_text(json.dumps(audit))
    return DOWNLOAD.apply_mirrors(records, path)


def test_exact_audit_matches_six_weights_and_preserves_hf_people(tmp_path, mirror_audit):
    records = audit_records(mirror_audit)
    group, repo, revision, names = DOWNLOAD.SPECS[2]
    people = {"group": group, "repo": repo, "revision": revision, "filename": names[0],
              "bytes": 12, "expected_sha256": "a" * 64}
    records.append(people)
    original = copy.deepcopy(records)
    result = apply_audit(tmp_path, mirror_audit, records)
    assert records == original
    assert result[-1] == people
    assert sum("mirror_url" in record for record in result) == 6
    for before, after in zip(records, result):
        assert before["expected_sha256"] == after["expected_sha256"]
    branch = next(record for record in result if "linear_branch" in record["filename"])
    assert "FilePath=stage-dmd-step-250%2Flinear_branch%2Fmodel.safetensors" in branch["mirror_url"]


@pytest.mark.parametrize("section,field,value", [
    ("planned_huggingface", "repo", "attacker/model"),
    ("planned_huggingface", "revision", "a" * 40),
    ("planned_huggingface", "filename", "other.safetensors"),
    ("planned_huggingface", "size_bytes", 1),
    ("planned_huggingface", "sha256", "a" * 64),
    ("modelscope", "repo", "attacker/model"),
    ("modelscope", "revision", "master"),
    ("modelscope", "filename", "../other.safetensors"),
    ("modelscope", "size_bytes", 1),
    ("modelscope", "sha256", "a" * 64),
    ("modelscope", "download_url", "http://127.0.0.1/private"),
    ("modelscope", "download_url", "https://modelscope.cn.evil.example/weight"),
])
def test_reject_tampered_mirror(tmp_path, mirror_audit, section, field, value):
    records = audit_records(mirror_audit)
    mirror_audit["records"][0][section][field] = value
    with pytest.raises(ValueError):
        apply_audit(tmp_path, mirror_audit, records)
    assert all("mirror_url" not in record for record in records)


def test_reject_duplicate_or_unapproved_mirrors(tmp_path, mirror_audit):
    records = audit_records(mirror_audit)
    duplicate = copy.deepcopy(mirror_audit)
    duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        apply_audit(tmp_path, duplicate, records)
    mirror_audit["records"][0]["recommend_alternate_source"] = False
    with pytest.raises(ValueError, match="approved"):
        apply_audit(tmp_path, mirror_audit, records)


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None, fail_after_read=False):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}
        self.fail_after_read = fail_after_read
        self.read_count = 0

    def read(self, size=-1):
        if self.fail_after_read and self.read_count:
            raise OSError("interrupted test transport")
        self.read_count += 1
        return super().read(size)


def tiny_record(group="a_lightx2v", mirrored=False, payload=b"0123456789"):
    _, repo, revision, names = next(spec for spec in DOWNLOAD.SPECS if spec[0] == group)
    record = {"group": group, "repo": repo, "revision": revision, "filename": names[0],
              "bytes": len(payload), "expected_sha256": hashlib.sha256(payload).hexdigest()}
    if mirrored:
        record["mirror_url"] = DOWNLOAD.mirror_identity(record)[3]
    return record


def test_mirror_uses_only_no_proxy_opener_and_hf_keeps_urlopen(tmp_path, monkeypatch):
    calls = []

    def build_opener(handler):
        assert handler.proxies == {}

        def direct(request, **kwargs):
            calls.append(("direct", request.full_url))
            return Response(b"0123456789")

        return SimpleNamespace(open=direct)

    def proxy(request, **kwargs):
        calls.append(("existing_proxy", request.full_url))
        return Response(b"0123456789")

    monkeypatch.setattr(DOWNLOAD.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", proxy)
    DOWNLOAD.download_file(tiny_record(mirrored=True), tmp_path / "a", lambda value: None)
    DOWNLOAD.download_file(tiny_record("c_realism"), tmp_path / "c", lambda value: None)
    assert calls[0][0] == "direct" and calls[0][1].startswith("https://modelscope.cn/api/")
    assert calls[1][0] == "existing_proxy" and calls[1][1].startswith("https://huggingface.co/fal/")
    record = tiny_record("c_realism")
    record["mirror_url"] = calls[0][1]
    with pytest.raises(ValueError, match="not approved"):
        DOWNLOAD.download_file(record, tmp_path / "rejected", lambda value: None)


def test_strict_resume_of_existing_partial(tmp_path, monkeypatch):
    record = tiny_record(mirrored=True)
    partial = tmp_path / (record["filename"] + ".part")
    partial.write_bytes(b"0123")

    def direct(request, **kwargs):
        assert request.get_header("Range") == "bytes=4-9"
        assert request.get_header("Accept-encoding") == "identity"
        return Response(b"456789", 206, {"Content-Range": "bytes 4-9/10", "Content-Length": "6"})

    monkeypatch.setattr(DOWNLOAD.urllib.request, "build_opener", lambda handler: SimpleNamespace(open=direct))
    progress = []
    target = DOWNLOAD.download_file(record, tmp_path, progress.append)
    assert target.read_bytes() == b"0123456789"
    assert progress == [10]
    assert not partial.exists()


@pytest.mark.parametrize("status,headers", [
    (200, {"Content-Length": "10"}),
    (206, {"Content-Range": "bytes 0-9/10", "Content-Length": "6"}),
    (206, {"Content-Range": "bytes 4-9/11", "Content-Length": "6"}),
    (206, {"Content-Range": "bytes 4-8/10", "Content-Length": "5"}),
    (206, {"Content-Range": "bytes 4-9/*", "Content-Length": "6"}),
    (206, {"Content-Range": "bytes 4-9/10garbage", "Content-Length": "6"}),
    (206, {"Content-Range": "bytes 4-9/10", "Content-Length": "7"}),
    (206, {"Content-Range": "bytes 4-9/10", "Content-Length": "6", "Content-Encoding": "gzip"}),
])
def test_bad_resume_headers_do_not_touch_partial(tmp_path, monkeypatch, status, headers):
    record = tiny_record()
    partial = tmp_path / (record["filename"] + ".part")
    partial.write_bytes(b"0123")
    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", lambda *args, **kwargs: Response(b"456789", status, headers))
    with pytest.raises(ValueError):
        DOWNLOAD.download_file(record, tmp_path, lambda value: None)
    assert partial.read_bytes() == b"0123"
    assert not (tmp_path / record["filename"]).exists()


def test_interrupted_transfer_resumes_single_partial(tmp_path, monkeypatch):
    record = tiny_record()
    requests = []

    def open_url(request, **kwargs):
        requests.append(request.get_header("Range"))
        if len(requests) == 1:
            return Response(b"0123", headers={"Content-Length": "10"}, fail_after_read=True)
        return Response(b"456789", 206, {"Content-Range": "bytes 4-9/10", "Content-Length": "6"})

    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(DOWNLOAD.time, "sleep", lambda value: None)
    target = DOWNLOAD.download_file(record, tmp_path, lambda value: None)
    assert requests == [None, "bytes=4-9"]
    assert target.read_bytes() == b"0123456789"


@pytest.mark.parametrize("already_complete_partial", [False, True])
def test_hash_failure_never_promotes_partial(tmp_path, monkeypatch, already_complete_partial):
    record = tiny_record()
    partial = tmp_path / (record["filename"] + ".part")
    wrong = b"xxxxxxxxxx"
    if already_complete_partial:
        partial.write_bytes(wrong)

    def open_url(*args, **kwargs):
        assert not already_complete_partial
        return Response(wrong)

    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", open_url)
    with pytest.raises(ValueError, match="hash mismatch"):
        DOWNLOAD.download_file(record, tmp_path, lambda value: None)
    assert partial.read_bytes() == wrong
    assert not (tmp_path / record["filename"]).exists()


def test_completed_target_hash_failure_is_not_overwritten(tmp_path):
    record = tiny_record()
    target = tmp_path / record["filename"]
    target.write_bytes(b"xxxxxxxxxx")
    with pytest.raises(ValueError, match="completed weight hash mismatch"):
        DOWNLOAD.download_file(record, tmp_path, lambda value: None)
    assert target.read_bytes() == b"xxxxxxxxxx"


def test_second_writer_cannot_open_same_partial(tmp_path, monkeypatch):
    record = tiny_record()
    partial = tmp_path / (record["filename"] + ".part")
    partial.write_bytes(b"0123")
    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("must not request"))
    with DOWNLOAD.exclusive_lock(tmp_path / (record["filename"] + ".part.lock")):
        with pytest.raises(RuntimeError, match="locked"):
            DOWNLOAD.download_file(record, tmp_path, lambda value: None)
    assert partial.read_bytes() == b"0123"


def test_root_lock_prevents_report_and_writer_races(tmp_path):
    with DOWNLOAD.exclusive_lock(tmp_path / ".download.lock"):
        with pytest.raises(RuntimeError, match="locked"):
            with DOWNLOAD.exclusive_lock(tmp_path / ".download.lock"):
                pytest.fail("second downloader entered")
    with DOWNLOAD.exclusive_lock(tmp_path / ".download.lock"):
        pass


def test_cli_mirror_manifest_preflight_without_download(tmp_path, mirror_audit, monkeypatch, capsys):
    audit_path = tmp_path / "mirrors.json"
    audit_path.write_text(json.dumps(mirror_audit))
    records = audit_records(mirror_audit)
    monkeypatch.setattr(DOWNLOAD, "cached_manifest", lambda path: records)
    monkeypatch.setattr(DOWNLOAD.shutil, "disk_usage", lambda path: SimpleNamespace(free=2**50))
    monkeypatch.setattr(DOWNLOAD.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("preflight must not download"))
    monkeypatch.setattr(sys, "argv", ["download_comparison_models.py", "--root", str(tmp_path / "cache"),
                                     "--manifest-file", "frozen-plan.json", "--mirror-manifest", str(audit_path)])
    DOWNLOAD.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "preflight"
    assert sum("mirror_url" in record for record in report["files"]) == 6
    assert all(record.get("status") != "downloading" for record in report["files"])


def test_cached_manifest_rejects_duplicate_destination(tmp_path):
    records = []
    for group, repo, revision, names in DOWNLOAD.SPECS:
        for name in names:
            records.append({"group": group, "repo": repo, "revision": revision,
                            "filename": name, "bytes": 12, "expected_sha256": "a" * 64})
    records.append(copy.deepcopy(records[0]))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"files": records}))
    with pytest.raises(ValueError, match="duplicate"):
        DOWNLOAD.cached_manifest(path)
