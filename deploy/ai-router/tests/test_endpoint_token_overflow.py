"""The backend's render rejection can still provide useful overflow evidence."""

import asyncio
from types import SimpleNamespace

import httpx

from ai_router.endpoint_tokens import EndpointTokenCounter


def test_render_context_rejection_preserves_input_lower_bound():
    async def run():
        endpoint = SimpleNamespace(
            metadata={"token_counting": {"enabled": True, "method": "render", "version": "test"}},
            api_base="http://localhost/v1", provider_model="local-test", backend_api_key_env="",
        )
        message = ("This model's maximum context length is 262144 tokens. "
                   "Your prompt contains at least 196609 input tokens.")
        responses = [
            httpx.Response(400, json={"error": {"message": message}}),
            httpx.Response(400, json={"error": {"message": "invalid request"}}),
            httpx.Response(503, json={"error": {"message": message}}),
        ]
        counter = EndpointTokenCounter()
        await counter.close()
        counter.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: responses.pop(0)))
        try:
            body = {"messages": [{"role": "user", "content": "task"}]}
            overflow = await counter.count(endpoint, body, "chat", 100)
            assert overflow["reason"] == "backend_context_exceeded"
            assert overflow["prompt_tokens_lower_bound"] == 196609
            assert overflow["tokens"] == 196609
            assert overflow["exact"] is False
            invalid = await counter.count(endpoint, body, "chat", 100)
            unavailable = await counter.count(endpoint, body, "chat", 100)
            assert invalid["reason"] == unavailable["reason"] == "backend_tokenization_unavailable"
            assert invalid["tokens"] == unavailable["tokens"] == 100
            assert [invalid["http_status"], unavailable["http_status"]] == [400, 503]
        finally:
            await counter.close()

    asyncio.run(run())
