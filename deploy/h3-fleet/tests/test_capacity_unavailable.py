import asyncio

import httpx
import pytest

from test_main import load_module


@pytest.mark.parametrize("failure", ["offline", "invalid_json", "http_error"])
def test_unreadable_worker_capacity_is_unavailable_not_empty(tmp_path, failure):
    module = load_module(tmp_path)
    def backend(request):
        if failure == "offline":
            raise httpx.ConnectError("private worker address", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"invalid")
        return httpx.Response(502, content=b"private worker failure")
    async def run():
        original = module.fleet.client
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
            module.fleet.client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
                response = await client.get("/api/router/capacity", headers={"Authorization": "Bearer test-router-key"})
                assert response.status_code == 503
                assert response.json() == {"detail": "capacity_unavailable"}
                assert module.fleet.store.active() == []
        await original.aclose()
    asyncio.run(run())
