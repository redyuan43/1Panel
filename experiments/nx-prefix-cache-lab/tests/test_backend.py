from prefix_cache_lab import backend
from prefix_cache_lab.config import NodeConfig


def test_direct_client_sends_lab_api_key(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    node = NodeConfig(
        name="nx3",
        ssh_host="nx3",
        base_url="http://nx3",
        backend_api_key_env="LAB_BACKEND_KEY",
        lab_api_key_file="/state/api-key",
    )
    monkeypatch.setattr(backend, "load_lab_api_key", lambda _node: "secret-key")
    monkeypatch.setattr(backend.httpx, "Client", FakeClient)

    client = backend.DirectLlamaClient(node)
    client.close()

    assert captured["headers"] == {"Authorization": "Bearer secret-key"}
