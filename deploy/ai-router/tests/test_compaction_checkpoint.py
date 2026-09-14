import asyncio
import json
import sqlite3
import time

from cryptography.fernet import Fernet
import httpx
import pytest

from ai_router.compaction import CapsuleCipher
from ai_router.compaction import SummaryResponseError
from ai_router.compaction_checkpoint import CheckpointedCompactor
from ai_router.compaction_jobs import CompactionJobs, CompactionJobConflict
from ai_router.errors import CompactionUnavailableError


@pytest.mark.parametrize("backend", ["checkpoint", "foreground"])
def test_provider_reasoning_usage_consumes_task_output_budget(tmp_path, backend):
    from ai_router.compaction import ContextCompactor, SummaryWork
    async def scenario():
        key = Fernet.generate_key().decode()
        limits = {"max_output_tokens": 10000}
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"facts": ["preserved"]})}}],
                "usage": {"completion_tokens": 6000, "completion_tokens_details": {"reasoning_tokens": 5900}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            kwargs = dict(internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary", client=client)
            first = [{"role": "user", "content": "first source"}]
            second = [{"role": "user", "content": "different source, not a cache hit"}]
            if backend == "checkpoint":
                jobs = CompactionJobs(tmp_path / "usage.sqlite3", key)
                job = jobs.create("alice", "branch", {"messages": first}, "chat", {"limits": limits})
                jobs.claim("worker")
                compactor = CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=jobs, job=job, worker="worker", **kwargs)
                await compactor._summarize(first)
                assert jobs.read("alice", job["id"])["output_tokens"] == 6000
                with pytest.raises(CompactionJobConflict, match="budget"):
                    await compactor._summarize(second)
            else:
                compactor = ContextCompactor(Counter(), CapsuleCipher(key), work_limits=limits, **kwargs)
                work = SummaryWork(started_at=time.monotonic())
                await compactor._summarize_bounded(first, target=None, input_budget=10000, work=work)
                assert work.output_tokens == 6000
                with pytest.raises(CompactionUnavailableError, match="workload"):
                    await compactor._summarize_bounded(second, target=None, input_budget=10000, work=work)
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [None, 0, -1, True, "6000", 6000, 9000])
def test_summary_usage_fallback_is_conservative_and_not_in_history(tmp_path, reported):
    async def scenario():
        key = Fernet.generate_key().decode()
        jobs = CompactionJobs(tmp_path / "fallback.sqlite3", key)
        job = jobs.create("alice", "branch", {"messages": []}, "chat", {})
        jobs.claim("worker")
        def upstream(request):
            payload = {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"facts": ["source fact"]})}}]}
            if reported is not None:
                payload["usage"] = {"completion_tokens": reported}
            return httpx.Response(200, json=payload)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            compactor = CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=jobs, job=job, worker="worker",
                internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary", client=client)
            if reported == 9000:
                with pytest.raises(SummaryResponseError) as failure:
                    await compactor._summarize([{"role": "user", "content": "source"}])
                assert failure.value.reason_code == "invalid_usage"
            else:
                result = await compactor._summarize([{"role": "user", "content": "source"}])
                assert "output_tokens" not in json.loads(json.dumps(result))
                assert jobs.read("alice", job["id"])["output_tokens"] == (6000 if reported == 6000 else 8192)
    asyncio.run(scenario())


class Counter:
    def count_request(self, body, kind):
        return len(json.dumps(body)) // 4 + 1


@pytest.mark.parametrize("finish", ["aborted", "insufficient_system_resource"])
def test_interrupted_json_summary_never_completes_checkpoint(tmp_path, monkeypatch, finish):
    from unittest.mock import Mock
    async def scenario():
        key = Fernet.generate_key().decode()
        jobs = CompactionJobs(tmp_path / "interrupted.sqlite3", key)
        job = jobs.create("alice", "branch", {"messages": []}, "chat", {})
        jobs.claim("worker")
        complete = Mock(wraps=jobs.complete_step)
        monkeypatch.setattr(jobs, "complete_step", complete)
        def upstream(request):
            return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                "message": {"content": '{"facts":["partial but valid JSON"]}'}}],
                "usage": {"completion_tokens": 12}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            value = CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=jobs, job=job,
                worker="worker", internal_base_url="http://isolated.invalid", internal_api_key="",
                model_id="summary", client=client)
            with pytest.raises(SummaryResponseError) as failure:
                await value._summarize([{"role": "user", "content": "source"}])
            assert failure.value.reason_code == "incomplete_response"
            assert not failure.value.retryable
            complete.assert_not_called()
            state = jobs.read("alice", job["id"])
            assert not jobs.public(state)["unresolved_operation_ids"]
            assert next(iter(state["steps"].values()))["state"] == "failed"
    asyncio.run(scenario())


def test_real_summary_function_reuses_saved_result_after_restart(tmp_path):
    async def scenario():
        key = Fernet.generate_key().decode()
        jobs = CompactionJobs(tmp_path / "jobs.sqlite3", key)
        messages = [{"role": "user", "content": "preserve this source fact"}]
        job = jobs.create("alice", "branch", {"messages": messages}, "chat", {"model": "summary"})
        jobs.claim("worker-1")
        operations = []
        def upstream(request):
            operations.append(request.headers["X-1Panel-Operation-ID"])
            assert request.headers["X-1Panel-Operation-Kind"] == "background_compaction"
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"facts": ["preserved source fact"]})}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            def compactor(store, worker):
                return CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=store, job=job, worker=worker,
                    internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary", client=client)
            first = await compactor(jobs, "worker-1")._summarize(messages)
            with sqlite3.connect(jobs.path) as db:
                db.execute("UPDATE compaction_jobs SET lease_until=0")
            restarted = CompactionJobs(jobs.path, key)
            restarted.claim("worker-2")
            second = await compactor(restarted, "worker-2")._summarize(messages)
            assert first == second and len(operations) == 1
            state = restarted.read("alice", job["id"])
            assert state["calls"] == 1 and state["output_tokens"] > 0
    asyncio.run(scenario())


def test_timeout_leaves_unknown_dispatch_and_does_not_resend(tmp_path):
    async def scenario():
        key = Fernet.generate_key().decode()
        jobs = CompactionJobs(tmp_path / "jobs.sqlite3", key)
        job = jobs.create("alice", "branch", {"messages": []}, "chat", {})
        jobs.claim("worker")
        calls = []
        def upstream(request):
            calls.append(request)
            raise httpx.ReadTimeout("synthetic timeout")
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            compactor = CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=jobs, job=job, worker="worker",
                internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary", client=client)
            with pytest.raises(CompactionUnavailableError):
                await compactor._summarize([{"role": "user", "content": "source"}])
            with pytest.raises(CompactionJobConflict, match="unknown"):
                await compactor._summarize([{"role": "user", "content": "source"}])
            assert len(calls) == 1
    asyncio.run(scenario())


def test_elapsed_time_budget_survives_reclaim(tmp_path, monkeypatch):
    import ai_router.compaction_jobs as module
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    key = Fernet.generate_key().decode()
    jobs = CompactionJobs(tmp_path / "jobs.sqlite3", key)
    job = jobs.create("alice", "branch", {"messages": []}, "chat", {})
    jobs.claim("first", ttl=300)
    now[0] += 250
    jobs.heartbeat(job["id"], "first", ttl=300)
    now[0] += 301
    assert jobs.claim("second", ttl=300)["elapsed_seconds"] == 550
    now[0] += 51
    with pytest.raises(CompactionJobConflict, match="budget"):
        jobs.dispatch(job["id"], "second", "new-step", 100, 100)


@pytest.mark.parametrize("status", [429, 503])
def test_confirmed_http_failure_can_retry_once_with_new_operation(tmp_path, status):
    async def scenario():
        key = Fernet.generate_key().decode()
        jobs = CompactionJobs(tmp_path / "jobs.sqlite3", key)
        job = jobs.create("alice", "branch", {"messages": []}, "chat", {})
        jobs.claim("worker")
        operations = []
        def upstream(request):
            operations.append(request.headers["X-1Panel-Operation-ID"])
            if len(operations) == 1:
                return httpx.Response(status, headers={"retry-after": "0"}, json={"error": "synthetic"})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"facts":["saved"]}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            compactor = CheckpointedCompactor(Counter(), CapsuleCipher(key), jobs=jobs, job=job, worker="worker",
                internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary", client=client)
            messages = [{"role": "user", "content": "source"}]
            with pytest.raises(SummaryResponseError) as error:
                await compactor._summarize(messages)
            assert error.value.retryable
            state = jobs.read("alice", job["id"])
            assert next(iter(state["steps"].values()))["state"] == "failed"
            assert (await compactor._summarize(messages))["facts"] == ["saved"]
            await compactor._summarize(messages)
            state = jobs.read("alice", job["id"])
            assert state["calls"] == 2 and len(set(operations)) == 2
            step = next(iter(state["steps"].values()))
            assert step["previous_attempts"] == [{"operation_id": operations[0], "status_code": status}]
    asyncio.run(scenario())


@pytest.mark.parametrize("delay", ["NaN", "inf", "-1", "61", "Wed, 21 Oct 2030 07:28:00 GMT"])
def test_retry_after_is_not_ignored_or_shortened(delay):
    assert not SummaryResponseError(503, "test", delay).retryable
