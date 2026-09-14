import sqlite3

from cryptography.fernet import Fernet
import pytest

from ai_router.compaction_jobs import CompactionJobs, CompactionJobConflict


@pytest.fixture
def jobs(tmp_path):
    key = Fernet.generate_key().decode()
    return CompactionJobs(tmp_path / "jobs.sqlite3", key), key


def create(store, owner="alice", text="private-job-canary-782"):
    return store.create(owner, "branch-1", {"messages": [{"role": "user", "content": text}]},
                        "chat", {"model": "summary", "window": 32000})


def expire(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE compaction_jobs SET lease_until=0 WHERE state='running'")


def test_job_dedupe_owner_isolation_and_ciphertext(jobs):
    store, _ = jobs
    first = create(store)
    assert create(store)["id"] == first["id"]
    assert create(store, "bob")["id"] != first["id"]
    assert store.read("bob", first["id"]) is None
    raw = store.path.read_bytes()
    for secret in (b"private-job-canary-782", b"alice", b"branch-1"):
        assert secret not in raw


def test_two_processes_cannot_claim_parallel_jobs(jobs):
    store, key = jobs
    create(store)
    create(store, "bob")
    other = CompactionJobs(store.path, key)
    assert store.claim("worker-1")
    assert other.claim("worker-2") is None


@pytest.mark.parametrize("reason,expected", [("invalid_fields", "invalid_fields"),
    ("private upstream text", "invalid_response")])
def test_failed_summary_preserves_safe_reason_across_restart(jobs, reason, expected):
    store, key = jobs
    job = create(store)
    store.claim("worker")
    store.dispatch(job["id"], "worker", "input-hash", 100, 50)
    store.failed_step(job["id"], "worker", "input-hash", 200, False, reason)
    store.fail(job["id"], "worker", "generic outer error")
    state = CompactionJobs(store.path, key).read("alice", job["id"])
    assert state["error"] == f"summary_http_200_{expected}"
    assert state["first_error"]["reason_code"] == expected
    assert state["first_error"]["operation_id"] == state["steps"]["input-hash"]["operation_id"]
    assert state["state"] == "failed"
    assert "private upstream text" not in str(state)


def test_restart_resumes_completed_steps_without_resending(jobs):
    store, key = jobs
    job = create(store)
    store.claim("worker-1")
    assert store.dispatch(job["id"], "worker-1", "input-hash", 100, 50) is None
    store.complete_step(job["id"], "worker-1", "input-hash", {"facts": ["saved"]}, 10)
    expire(store)
    restarted = CompactionJobs(store.path, key)
    assert restarted.claim("worker-2")["id"] == job["id"]
    assert restarted.dispatch(job["id"], "worker-2", "input-hash", 100, 50) == {"facts": ["saved"]}
    assert restarted.read("alice", job["id"])["calls"] == 1


def test_unknown_outcome_is_not_automatically_replayed(jobs):
    store, key = jobs
    job = create(store)
    store.claim("old-worker")
    store.dispatch(job["id"], "old-worker", "input-hash", 100, 50)
    with pytest.raises(CompactionJobConflict, match="unknown"):
        store.dispatch(job["id"], "old-worker", "input-hash", 100, 50)
    expire(store)
    restarted = CompactionJobs(store.path, key)
    assert restarted.claim("new-worker") is None
    assert restarted.read("alice", job["id"])["state"] == "needs_context"
    with pytest.raises(CompactionJobConflict):
        store.complete_step(job["id"], "old-worker", "input-hash", {"facts": ["late"]}, 10)


def test_candidates_only_apply_to_exact_original_branch_prefix(jobs):
    store, _ = jobs
    job = create(store)
    store.claim("worker")
    summary = [{"role": "user", "content": "validated summary"}]
    store.candidate(job["id"], "worker", summary)
    original = job["body"]["messages"]
    incoming = {"messages": [*original, {"role": "user", "content": "new request"}], "tools": []}
    result = store.apply_candidate("alice", job["id"], "branch-1", incoming, "chat")
    assert result["messages"] == [*summary, incoming["messages"][-1]]
    assert incoming["messages"][0]["content"] == "private-job-canary-782"
    assert result["tools"] == []
    assert store.apply_candidate("alice", job["id"], "other-branch", incoming, "chat") is None
    assert store.apply_candidate("bob", job["id"], "branch-1", incoming, "chat") is None
    incoming["messages"][0] = {"role": "user", "content": "edited history"}
    assert store.apply_candidate("alice", job["id"], "branch-1", incoming, "chat") is None


def test_cancel_fences_worker_and_disables_candidate(jobs):
    store, _ = jobs
    job = create(store)
    store.claim("worker")
    assert not store.cancel("bob", job["id"])
    assert store.cancel("alice", job["id"])
    with pytest.raises(CompactionJobConflict):
        store.candidate(job["id"], "worker", [{"role": "user", "content": "stale summary"}])
    assert store.apply_candidate("alice", job["id"], "branch-1", job["body"], "chat") is None


def test_budget_is_checked_before_dispatch_intent(jobs):
    store, _ = jobs
    job = create(store)
    store.claim("worker")
    with pytest.raises(CompactionJobConflict, match="budget"):
        store.dispatch(job["id"], "worker", "input-hash", 1_000_001, 8192)
    value = store.read("alice", job["id"])
    assert value["calls"] == 0 and value["steps"] == {}


def test_cancelled_inflight_request_blocks_new_dispatch_until_reconciled(jobs):
    store, _ = jobs
    job = create(store)
    create(store, "bob")
    store.claim("worker")
    store.dispatch(job["id"], "worker", "step", 100, 50)
    store.cancel("alice", job["id"])
    value = store.read("alice", job["id"])
    assert value["cancel_requested"] and value["state"] == "needs_context"
    assert store.claim("next-worker") is None
    with pytest.raises(CompactionJobConflict):
        store.operation(job["id"], "worker", "step")


def test_operator_reconciliation_discards_without_replaying_or_erasing_source(jobs):
    store, _ = jobs
    job = create(store)
    later = create(store, "bob")
    store.claim("worker")
    store.dispatch(job["id"], "worker", "step", 100, 50)
    operation = store.operation(job["id"], "worker", "step")
    store.cancel("alice", job["id"])
    assert store.public(store.read("alice", job["id"]))["unresolved_operation_ids"] == [operation]
    for owner, operation_id in [("bob", operation), ("alice", "wrong-operation")]:
        with pytest.raises(CompactionJobConflict):
            store.abandon_verified_operation(owner, job["id"], operation_id, "log-reference-782")
    with pytest.raises(ValueError):
        store.abandon_verified_operation("alice", job["id"], operation, "")
    result = store.abandon_verified_operation("alice", job["id"], operation, "private-log-reference-782")
    assert result["state"] == "cancelled" and result["unresolved_operation_ids"] == []
    assert store.read("alice", job["id"])["body"] == job["body"]
    assert create(store)["id"] == job["id"]
    assert store.read("alice", job["id"])["calls"] == 1
    assert store.claim("next-worker")["id"] == later["id"]
    assert b"private-log-reference-782" not in store.path.read_bytes()
