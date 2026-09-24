"""A disabled master switch must stop the next summary model call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_router.api import _compact_body_for_target, _compaction_allowed
from ai_router.compaction_policy import require_compaction_enabled
from ai_router.errors import CompactionUnavailableError
from ai_router.identity import IdentityProfile
from ai_router.types import ModelCallTarget
from test_context_policy import setup as context_setup


@pytest.mark.parametrize("strategy", ["legacy", "compact", "extended"])
def test_disabled_switch_overrides_context_strategy(strategy):
    runtime = SimpleNamespace(settings=SimpleNamespace(section=lambda _: {"enabled": False}))
    assert not _compaction_allowed(runtime, True, "true", context_strategy=strategy)
    with pytest.raises(CompactionUnavailableError):
        require_compaction_enabled(runtime.settings)


def test_switch_closed_during_foreground_admission_stops_summary(tmp_path, monkeypatch):
    async def scenario():
        runtime = context_setup.__wrapped__(tmp_path)
        lease = SimpleNamespace(release=AsyncMock())
        runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
        runtime.compactor = SimpleNamespace(model_id="ai-qwen38-27b", summary_request_tokens=lambda _: 100,
                                           compact=AsyncMock(), summary_output_tokens=8192)

        async def acquire(*args, **kwargs):
            runtime.settings.write_runtime({"compaction": {"enabled": False}})
            return ModelCallTarget("http://offline.invalid", "summary", safe_context_tokens=16384)

        monkeypatch.setattr("ai_router.api._acquire_internal_model", acquire)
        with pytest.raises(CompactionUnavailableError, match="disabled"):
            await _compact_body_for_target(runtime, {"messages": []}, api_kind="chat", request_id="test",
                                           target_context=16384, identity=IdentityProfile.from_settings({}))
        runtime.compactor.compact.assert_not_awaited()
        lease.release.assert_awaited_once()

    asyncio.run(scenario())
