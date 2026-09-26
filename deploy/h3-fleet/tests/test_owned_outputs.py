import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.owned_outputs import owned_output


def test_completed_output_path_stays_inside_pinned_root(tmp_path):
    root = tmp_path / "output"
    folder = root / "video/router"
    folder.mkdir(parents=True)
    target = folder / "clip.mp4"
    target.write_bytes(b"video")
    assert owned_output(str(root), "clip.mp4", "video/router") == target
    for filename, subfolder in (("../clip.mp4", "video/router"),
                                ("clip.mp4", "../other"),
                                ("/tmp/clip.mp4", "video/router")):
        with pytest.raises(ValueError):
            owned_output(str(root), filename, subfolder)
    link = root / "alias"
    link.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError):
        owned_output(str(root), "clip.mp4", "alias")


def test_fleet_view_serves_completed_file_after_worker_stops(tmp_path, monkeypatch):
    from test_main import load_module
    main = load_module(tmp_path)
    root = tmp_path / "output"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"0123456789")
    job = {"status": "completed", "backend_json": "{}"}
    store = SimpleNamespace(
        find_output=lambda filename, folder, kind: job if (filename, folder, kind) ==
        ("clip.mp4", "", "output") else None,
        list=lambda **kwargs: [],
    )
    fake = SimpleNamespace(
        store=store,
        recipes=SimpleNamespace(backends={}, lane_for=lambda candidate: SimpleNamespace(id="fast")),
        client=SimpleNamespace(send=AsyncMock(side_effect=AssertionError("worker is stopped"))),
    )
    monkeypatch.setattr(main, "fleet", fake)
    monkeypatch.setenv("H3_PERSISTENT_OUTPUT_ROOTS", '{"fast": "' + str(root) + '"}')
    app = FastAPI()
    app.add_api_route("/view", main.view, methods=["GET"])
    with TestClient(app) as client:
        response = client.get("/view?filename=clip.mp4", headers={"Range": "bytes=2-5"})
        assert response.status_code == 206 and response.content == b"2345"
        job["status"] = "error"
        assert client.get("/view?filename=clip.mp4").status_code == 409
