import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.client_accounts import ClientAccountManager
from ai_router.control import create_app
from ai_router.errors import AuthenticationError
from ai_router.store import InMemoryStateStore
from ai_router.training_archive import TrainingArchive


def test_settings_preflight_checks_partial_update_against_current_runtime(tmp_path, monkeypatch):
    from pathlib import Path
    from ai_router.config import Settings

    settings = Settings(Path(__file__).resolve().parents[1] / "config/defaults.yaml",
                        tmp_path / "runtime.yaml")
    settings.write_runtime({"compaction": {"model_id": "stale-runtime-model"}})
    before = settings.runtime_path.read_bytes()
    runtime = SimpleNamespace(settings=settings, registry=Mock(), reload_settings=settings.reload)
    monkeypatch.setattr("ai_router.control._authorized_runtime", lambda request: runtime)
    inspected = []

    def validate(proposed, registry):
        inspected.append(proposed)
        raise ValueError("candidate inspected before write")

    monkeypatch.setattr("ai_router.control.validate_background_settings", validate)
    # No lifespan/model calls: only exercise the real settings HTTP handler.
    client = TestClient(create_app(runtime))
    try:
        result = client.put("/api/settings", json={"compaction": {"background_enabled": True}})
        assert result.status_code == 400
        assert inspected[0]["compaction"]["model_id"] == "stale-runtime-model"
        assert settings.runtime_path.read_bytes() == before
        candidate = {"compaction": {"background_enabled": False}}
        expected = settings.preview_runtime(candidate)
        settings.write_runtime(candidate)
        assert settings.value == expected
    finally:
        client.close()


def test_task_management_uses_owned_archive_and_redacts_job_bodies(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    settings = SimpleNamespace(runtime_path=tmp_path / "runtime.yaml", section=lambda name: {"background_enabled": True})
    accounts = ClientAccountManager(InMemoryStateStore(), settings, key)
    asyncio.run(accounts.create_account(dict(id="alice", name="Alice", models=["siyuan/auto"],
        rpm_limit=10, tpm_limit=10000, max_parallel_requests=1, allow_compaction=True), allowed_models={"siyuan/auto"}))
    record, _ = asyncio.run(accounts.create_key("alice", "test"))
    archive_key = tmp_path / "archive.key"
    archive_key.write_bytes(Fernet.generate_key())
    archive_path = tmp_path / "archive.sqlite3"
    archive = TrainingArchive(str(archive_path), str(archive_key))
    asyncio.run(archive.begin(request_id="request-1", conversation_id="conversation", conversation_mode="stateful",
        client_id="alice", key_id=record["key_id"], protocol="chat", instance_id="test", boot_id="test",
        received_body={"messages": [{"role": "user", "content": "PRIVATE_JOB_SOURCE_782"}]}, history_source_local_only=True))
    monkeypatch.setenv("AI_ROUTER_TRAINING_DB_PATH", str(archive_path))
    monkeypatch.setenv("AI_ROUTER_TRAINING_KEY_PATH", str(archive_key))
    def authenticate(value):
        if value != "Bearer test-admin":
            raise AuthenticationError()
    endpoint = SimpleNamespace(id="summary", enabled=True, safe_context_tokens=32000, cloud=False)
    trace = AsyncMock(return_value={"client_id": "alice", "branch_id": "branch-1"})
    from ai_router.compaction import CapsuleCipher
    runtime = SimpleNamespace(settings=settings, clients=accounts, state_encryption_key=key,
        store=InMemoryStateStore(), conversations=SimpleNamespace(get=AsyncMock(return_value=None)),
        auth=SimpleNamespace(authenticate_admin=authenticate), audit=Mock(), start=AsyncMock(),
        route_traces=SimpleNamespace(get=trace), registry=SimpleNamespace(by_id=lambda name: endpoint),
        compactor=SimpleNamespace(model_id="summary", cipher=CapsuleCipher(key)))
    headers = {"Authorization": "Bearer test-admin"}
    url = "/api/clients/alice/compaction-jobs"
    with TestClient(create_app(runtime)) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers=headers).json() == {"jobs": []}
        assert not settings.runtime_path.with_name("compaction-jobs.sqlite3").exists()
        response = client.post(url, headers=headers, json={"request_id": "request-1", "target_endpoint_id": "summary",
            "summary_records": [{"version": 1, "owner": "alice", "branch": "forged"}]})
        assert response.status_code == 200, response.text
        job_id = response.json()["job"]["id"]
        assert "PRIVATE_JOB_SOURCE_782" not in response.text
        assert client.get(url, headers=headers).json()["jobs"][0]["id"] == job_id
        assert client.post(url.replace("alice", "bob") + f"/{job_id}/cancel", headers=headers).status_code == 404
        trace.return_value = {"client_id": "bob", "branch_id": "branch-1"}
        assert client.post(url, headers=headers, json={"request_id": "request-1", "target_endpoint_id": "summary"}).status_code == 404
        trace.return_value = {"client_id": "alice", "branch_id": "branch-1"}
        endpoint.cloud = True
        assert client.post(url, headers=headers, json={"request_id": "request-1", "target_endpoint_id": "summary"}).status_code == 403
        cancelled = client.post(url + f"/{job_id}/cancel", headers=headers)
        assert cancelled.json()["job"]["state"] == "cancelled"
        from ai_router.compaction_jobs import CompactionJobs
        jobs = CompactionJobs(settings.runtime_path.with_name("compaction-jobs.sqlite3"), key)
        assert jobs.read("alice", job_id)["parameters"]["summary_records"] == []
        pending = jobs.create("alice", "branch-2", {"messages": []}, "chat", {})
        jobs.claim("worker")
        jobs.dispatch(pending["id"], "worker", "step", 100, 50)
        operation = jobs.operation(pending["id"], "worker", "step")
        jobs.cancel("alice", pending["id"])
        reconcile = url + f"/{pending['id']}/reconcile"
        data = {"operation_id": operation, "evidence_reference": "log-reference-782"}
        assert client.post(reconcile, headers=headers, json=data).status_code == 400
        data.update(upstream_terminal_confirmed=True, discard_result=True)
        assert client.post(reconcile, json=data).status_code == 401
        assert client.post(reconcile.replace("alice", "bob"), headers=headers, json=data).status_code == 409
        result = client.post(reconcile, headers=headers, json=data)
        assert result.status_code == 200, result.text
        assert result.json()["job"]["state"] == "cancelled"
        assert "log-reference-782" not in result.text
        # Malformed or future-version policy cannot authorize historical cloud use.
        from ai_router.control import ArchiveReader
        original = ArchiveReader(str(archive_path), str(archive_key)).read("request-1")
        reader = Mock()
        reader.read.return_value = original
        monkeypatch.setattr("ai_router.control.ArchiveReader", Mock(return_value=reader))
        for source_policy in (None, [], "invalid", {"local_only": False},
                              {"version": 2, "local_only": False}, {"version": True, "local_only": False},
                              {"version": 1, "local_only": 0}):
            original["request"]["history_source_policy"] = source_policy
            denied = client.post(url, headers=headers, json={"request_id": "request-1", "target_endpoint_id": "summary"})
            assert denied.status_code == 403, denied.text
        original["request"]["history_source_policy"] = {"version": 1, "local_only": False}
        allowed = client.post(url, headers=headers, json={"request_id": "request-1", "target_endpoint_id": "summary"})
        assert allowed.status_code == 200, allowed.text
