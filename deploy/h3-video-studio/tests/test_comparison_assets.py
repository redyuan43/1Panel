from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


def test_comparison_settings_default_and_external_paths(tmp_path, monkeypatch):
    release = tmp_path / "immutable-release"
    monkeypatch.setenv("H3_STUDIO_ROOT", str(release))
    monkeypatch.delenv("H3_STUDIO_COMPARISON_RESULTS", raising=False)
    monkeypatch.delenv("H3_STUDIO_COMPARISON_PLANNED", raising=False)
    settings = get_settings()
    assert settings.frontend_root == release / "frontend"
    assert settings.comparison_results_root == release / "frontend/comparison-results"
    assert settings.comparison_planned_path == release / "frontend/comparison-planned.json"
    monkeypatch.setenv("H3_STUDIO_COMPARISON_RESULTS", "")
    monkeypatch.setenv("H3_STUDIO_COMPARISON_PLANNED", "")
    assert get_settings() == settings
    monkeypatch.setenv("H3_STUDIO_COMPARISON_RESULTS", str(tmp_path / "published"))
    monkeypatch.setenv("H3_STUDIO_COMPARISON_PLANNED", str(tmp_path / "plan.json"))
    external = get_settings()
    assert external.frontend_root == settings.frontend_root
    assert external.comparison_results_root == tmp_path / "published"
    assert external.comparison_planned_path == tmp_path / "plan.json"
    assert not release.exists()


@pytest.fixture
def published(tmp_path, monkeypatch):
    from app import main

    root = tmp_path / "published"
    root.mkdir()
    plan = tmp_path / "external-plan.json"
    plan.write_text('{"planned": "external"}')
    (root / "index.json").write_text('{"history": ["R0", "C0", "D4", "A8"]}')
    (root / "live.json").write_text('{"status": "external"}')
    (root / "R0.mp4").write_bytes(b"0123456789")
    (root / "private.py").write_text("not a public media asset")
    outside = tmp_path / "outside.json"
    outside.write_text('{"private": true}')
    (root / "escape.json").symlink_to(outside)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS,
        comparison_results_root=root, comparison_planned_path=plan))
    return TestClient(main.app), root, plan


def test_external_history_json_and_video_range_head_are_readonly(published):
    client, root, plan = published
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (root / "R0.mp4", root / "index.json", plan)}
    assert client.get("/comparison-results/index.json").json()["history"] == ["R0", "C0", "D4", "A8"]
    assert client.get("/comparison-planned.json").json() == {"planned": "external"}
    response = client.get("/comparison-results/R0.mp4", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    head = client.head("/comparison-results/R0.mp4")
    assert head.status_code == 200 and not head.content
    assert head.headers["content-length"] == "10"
    matched = client.get("/comparison-results/R0.mp4", headers={"Range": "bytes=2-5", "If-Range": head.headers["etag"]})
    assert matched.status_code == 206 and matched.content == b"2345"
    changed = client.get("/comparison-results/R0.mp4", headers={"Range": "bytes=2-5", "If-Range": '"old-etag"'})
    assert changed.status_code == 200 and changed.content == b"0123456789"
    assert client.head("/comparison-planned.json").status_code == 200
    assert client.get("/comparison-results/R0.mp4", headers={"Range": "bytes=20-30"}).status_code == 416
    for path in ("/comparison-results/R0.mp4", "/comparison-results/index.json", "/comparison-planned.json"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, path, content=b"overwrite").status_code == 405
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}


@pytest.mark.parametrize("filename", ["private.py", "missing.json", "escape.json", "%2e%2e/outside.json",
                                      "%252e%252e/outside.json", "sub%5cprivate.json"])
def test_comparison_files_cannot_escape_root_or_serve_code(published, filename):
    client, _, _ = published
    assert client.get("/comparison-results/" + filename).status_code == 404


def test_missing_external_plan_does_not_fall_back_to_bundled_plan(published):
    client, _, plan = published
    plan.unlink()
    assert client.get("/comparison-planned.json").status_code == 404


def test_recipes_entrypoint_retains_api_authentication(published, monkeypatch, tmp_path):
    from app import main

    client, _, _ = published
    key = tmp_path / "studio.key"
    key.write_text("test-key")
    monkeypatch.setenv("H3_STUDIO_KEY_FILE", str(key))
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(is_fleet=True, recipe_catalog=lambda: {"enabled": False, "recipes": []}))
    assert client.get("/api/recipes").status_code == 401
    response = client.get("/api/recipes", headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 200 and response.json()["enabled"] is False
