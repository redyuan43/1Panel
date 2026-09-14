import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest
import yaml

from ai_router.compaction_limits import CEILINGS, parse_limits
from ai_router.compaction_jobs import CompactionJobs, CompactionJobConflict
from ai_router.compaction import ContextCompactor, CapsuleCipher
from ai_router.config import validate_settings
from ai_router.errors import CompactionUnavailableError
from ai_router.token_counter import SimpleTokenCounter


@pytest.mark.parametrize("value", [None, [], {"unknown": 1}, {"max_calls": True}, {"max_calls": 0},
    {"max_calls": 33}, {"max_seconds": "10"}, {"max_seconds": 601},
    {"max_input_tokens": 1000001}, {"max_output_tokens": 8191}, {"max_output_tokens": 64001}])
def test_invalid_limits_rejected_by_settings(value):
    settings = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/defaults.yaml").read_text())
    settings["compaction"]["background_limits"] = value
    with pytest.raises(ValueError, match="compaction background"):
        validate_settings(settings)


@pytest.mark.parametrize("limits,first_input,first_output,next_input,next_output", [
    ({"max_calls": 1}, 100, 10, 100, 8192),
    ({"max_input_tokens": 150}, 100, 10, 51, 8192),
    ({"max_output_tokens": 8192}, 100, 1, 1, 8192),
])
def test_job_budget_is_snapshotted_and_enforced_after_reopen(tmp_path, limits, first_input, first_output, next_input, next_output):
    key = Fernet.generate_key().decode()
    store = CompactionJobs(tmp_path / "jobs.sqlite3", key)
    job = store.create("owner", "branch", {"messages": []}, "chat", {"limits": limits})
    expected = parse_limits(limits)
    limits.clear()
    store.claim("worker")
    store.dispatch(job["id"], "worker", "first", first_input, 8192)
    store.complete_step(job["id"], "worker", "first", {"facts": ["saved"]}, first_output)
    reopened = CompactionJobs(store.path, key)
    assert reopened.public(reopened.read("owner", job["id"]))["limits"] == expected
    with pytest.raises(CompactionJobConflict, match="budget"):
        reopened.dispatch(job["id"], "worker", "second", next_input, next_output)
    assert reopened.read("owner", job["id"])["calls"] == 1


def test_compactor_checks_custom_budget_before_model_call():
    async def scenario():
        compactor = ContextCompactor(SimpleTokenCounter(), CapsuleCipher(Fernet.generate_key().decode()),
            internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary",
            client=AsyncMock(), work_limits={"max_input_tokens": 1})
        compactor._summarize = AsyncMock()
        with pytest.raises(CompactionUnavailableError, match="bounded"):
            await compactor._summarize_bounded([{"role": "user", "content": "source"}],
                target=None, input_budget=10000)
        compactor._summarize.assert_not_awaited()
    asyncio.run(scenario())


def test_short_time_limit_survives_expired_lease_recovery(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("ai_router.compaction_jobs.time.time", lambda: clock[0])
    key = Fernet.generate_key().decode()
    store = CompactionJobs(tmp_path / "jobs.sqlite3", key)
    job = store.create("owner", "branch", {"messages": []}, "chat", {"limits": {"max_seconds": 1}})
    store.claim("worker")
    clock[0] += 2
    with pytest.raises(CompactionJobConflict, match="budget"):
        store.dispatch(job["id"], "worker", "first", 1, 8192)
    with pytest.raises(CompactionJobConflict, match="time budget"):
        store.candidate(job["id"], "worker", [{"role": "user", "content": "late"}])
    clock[0] += 60
    reopened = CompactionJobs(store.path, key)
    assert reopened.claim("new-worker") is None
    assert reopened.read("owner", job["id"])["state"] == "failed"


def test_legacy_defaults_and_valid_partial_limits():
    assert parse_limits({}) == CEILINGS
    assert parse_limits({"max_seconds": 30}) == {**CEILINGS, "max_seconds": 30}
