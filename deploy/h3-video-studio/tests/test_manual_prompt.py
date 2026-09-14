from dataclasses import replace

from fastapi.testclient import TestClient

from app.storage import BatchStore, ProjectStore


def test_manual_context_preserves_original_without_cloud(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, data_root=tmp_path))
    store = ProjectStore(tmp_path / "studio.sqlite3")
    batches = BatchStore(tmp_path / "studio.sqlite3")
    store.initialize()
    batches.initialize()
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "BATCH_STORE", batches)
    monkeypatch.setattr(main, "_spawn", lambda function, identifier: function(identifier))

    def forbid_cloud(*args, **kwargs):
        raise AssertionError("Cloud must not be called for manual prompts")

    monkeypatch.setattr(main.MINIMAX, "context_ir", forbid_cloud)
    client = TestClient(main.app)
    prompt = "integrated_multimodal_description:\n雨后街口，15秒竖版。"
    response = client.post("/api/projects", data={"mode": "t2v", "prompt": prompt,
                           "orientation": "portrait", "prompt_processing": "manual"})
    assert response.status_code == 200
    project = response.json()
    assert project["orientation"] == "portrait"
    identifier = project["id"]
    result = client.post(f"/api/projects/{identifier}/context-ir").json()
    assert result["prompt_ir"] == prompt
    assert result["stages"]["context_ir"]["status"] == "awaiting_approval"
    assert result["stages"]["context_ir"]["prompt_processing"] == "manual"
    assert client.post(f"/api/projects/{identifier}/context-ir/approve", json={"prompt": prompt}).status_code == 200
    assert store.get(identifier)["prompt_approved"] == prompt
