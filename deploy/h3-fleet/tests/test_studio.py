import asyncio

import httpx
import pytest

from test_main import load_module


def test_batch_window_excludes_other_batches_and_validation(tmp_path):
    module = load_module(tmp_path)
    store = module.fleet.store
    store.reserve_studio_batch("studio_batch_first")
    store.reserve_studio_batch("studio_batch_first")
    with pytest.raises(module.HTTPException) as denied:
        store.reserve_studio_batch("studio_batch_other")
    assert denied.value.status_code == 409
    with pytest.raises(module.HTTPException):
        store.set_validation_lease("validation", 60)
    store.release_studio_batch("studio_batch_first")
    store.set_validation_lease("validation", 60)
    with pytest.raises(module.HTTPException):
        store.reserve_studio_batch("studio_batch_first")


def test_batch_window_cannot_release_active_execution(tmp_path):
    module = load_module(tmp_path)
    store = module.fleet.store
    store.reserve_studio_batch("studio_batch_first")
    store.create(prompt_id="prompt", upstream_prompt_id="upstream", execution_id="studio_batch_first_item",
                 request_digest=None, lane_id="fast", stage="local_768", profile="quality")
    with pytest.raises(module.HTTPException):
        store.release_studio_batch("studio_batch_first")
    store.update("prompt", status="completed")
    store.release_studio_batch("studio_batch_first")
    assert store.studio_batch() is None


def test_immutable_input_rejects_worker_rename(tmp_path):
    module = load_module(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"name": "unexpected.png", "subfolder": ""})
        )) as client:
            module.fleet.client = client
            with pytest.raises(module.HTTPException) as denied:
                await module.replicate_input("original.png", b"test", "image/png", overwrite="false")
            assert denied.value.status_code == 502

    asyncio.run(scenario())
