"""Server-owned summary receipts; never infer ownership from message text alone."""
from __future__ import annotations

import hashlib
import json
import logging

from .compaction import HANDOFF_KEYS, extract_messages, replace_messages, message_hash, _handoff_message
from .errors import ConversationStateConflictError

log = logging.getLogger(__name__)


def system_hashes(body, api_kind):
    return frozenset(message_hash(item) for item in extract_messages(body, api_kind)
                     if str(item.get("role", "")).lower() in {"system", "developer"})


def _messages_digest(messages):
    return hashlib.sha256(json.dumps([message_hash(item) for item in messages]).encode()).hexdigest()


class SummaryScope:
    def __init__(self, *, owner, branch, api_kind, cipher, store=None, ttl=86400,
                 ancestors=(), protected=(), records=(), source_messages=None):
        self.owner, self.branch, self.api_kind = owner, branch, api_kind
        self.cipher, self.store, self.ttl = cipher, store, ttl
        self.branches = tuple(dict.fromkeys((branch, *ancestors)))[:33]
        self.protected = frozenset(protected)
        self.records = list(records)[-128:]
        self.source_prefix = (None if source_messages is None else {
            "count": len(source_messages), "digest": _messages_digest(source_messages)})

    def _key(self, branch):
        digest = hashlib.sha256(json.dumps([self.owner, branch, self.api_kind]).encode()).hexdigest()
        return "router:summary-provenance:" + digest

    async def load(self):
        if self.store is None:
            return
        for branch in self.branches:
            try:
                value = await self.store.get_json(self._key(branch))
            except Exception as exc:
                log.warning("summary_receipt_load_unavailable error_type=%s", type(exc).__name__)
                break
            if value:
                try:
                    record = self.cipher.decrypt(value["ciphertext"])
                    if self._authorized(record):
                        self.records.append(record)
                except (ValueError, KeyError, TypeError, ConversationStateConflictError):
                    # Missing/invalid receipts never authorize compaction.
                    continue

    def _authorized(self, record):
        return (isinstance(record, dict) and type(record.get("version")) is int and record.get("version") == 1
                and record.get("owner") == self.owner and record.get("branch") in self.branches
                and record.get("api_kind") == self.api_kind)

    def indices(self, messages):
        hashes = [message_hash(item) for item in messages]
        result = set()
        for record in self.records:
            if not self._authorized(record):
                continue
            prefix = record.get("prefix_hashes")
            if not isinstance(prefix, list) or not prefix or hashes[:len(prefix)] != prefix:
                continue
            indices = record.get("summary_indices", [])
            if not isinstance(indices, list):
                continue
            for index in indices:
                if (type(index) is int and 0 <= index < len(prefix)
                        and messages[index].get("role") == "system"
                        and hashes[index] not in self.protected):
                    result.add(index)
        return frozenset(result)

    def receipt(self, messages, indices):
        indices = sorted(set(indices))
        if not indices:
            return None
        return {"version": 1, "owner": self.owner, "branch": self.branch,
                "api_kind": self.api_kind,
                "prefix_hashes": [message_hash(item) for item in messages[:max(indices) + 1]],
                "summary_indices": indices}

    async def remember(self, messages, indices):
        record = self.receipt(messages, indices)
        if record is None:
            return
        if self.source_prefix is not None:
            # Optional encrypted metadata: old readers ignore these fields.
            # Only this live request can bind its raw source to its output;
            # adopting an older job must never manufacture this binding.
            record["source_prefix"] = dict(self.source_prefix)
            record["compacted_request_digest"] = _messages_digest(messages)
        self.records.append(record)
        self.records = self.records[-128:]
        if self.store is not None:
            try:
                await self.store.set_json(self._key(self.branch),
                    {"ciphertext": self.cipher.encrypt(record)}, ttl_seconds=self.ttl)
            except Exception as exc:
                # Optional metadata must not turn an already-dispatched answer
                # into an error/retry. Missing proof remains protected next turn.
                log.warning("summary_receipt_save_unavailable error_type=%s", type(exc).__name__)

    def restore_chat_parent(self, body, parent):
        """Restore only a receipt-proven full prefix, including its exact answer.

        A conversation ID and a matching last message alone do not prove that
        the client kept earlier instructions or tool results unchanged.
        Missing legacy evidence and edited history leave the request intact.
        """
        if self.api_kind != "chat" or parent is None or not parent.encrypted_capsule:
            return body
        incoming = extract_messages(body, "chat")
        for record in reversed(self.records):
            if not self._authorized(record) or record.get("branch") != parent.branch_id:
                continue
            prefix = record.get("source_prefix")
            if not isinstance(prefix, dict):
                continue
            count = prefix.get("count")
            if type(count) is not int or not 0 < count < len(incoming):
                continue
            if _messages_digest(incoming[:count]) != prefix.get("digest"):
                continue
            try:
                saved = self.cipher.decrypt(parent.encrypted_capsule)
            except (ValueError, TypeError, ConversationStateConflictError):
                continue
            if not isinstance(saved, list) or not saved or not all(isinstance(m, dict) for m in saved):
                continue
            if _messages_digest(saved[:-1]) != record.get("compacted_request_digest"):
                continue
            if (saved[-1].get("role") != "assistant"
                    or message_hash(saved[-1]) != parent.boundary_hash
                    or message_hash(incoming[count]) != parent.boundary_hash):
                continue
            # Use the proven position, not the last matching answer text: a
            # later identical answer must not swallow newly appended turns.
            return replace_messages(body, "chat", [*saved, *incoming[count + 1:]])
        return body

    def job_parameters(self):
        return {"summary_ancestors": list(self.branches[1:]),
                "summary_protected": sorted(self.protected), "summary_records": self.records}

    def adopt_legacy_job(self, job):
        """An encrypted ready job proves exactly the handoff it generated.

        Its pre-existing system messages are not implicitly Router-owned. Older
        handoffs require their own job receipt; ambiguity stays protected.
        """
        if (job.get("owner") != self.owner or job.get("branch") not in self.branches
                or job.get("api_kind") != self.api_kind or job.get("state") != "ready"):
            return
        original = extract_messages(job["body"], self.api_kind)
        candidate = job.get("candidate", [])
        # New jobs persist exact output indices inside their encrypted record.
        indices = job.get("summary_indices")
        if isinstance(indices, list) and indices and isinstance(candidate, list):
            if all(type(i) is int and 0 <= i < len(candidate)
                   and isinstance(candidate[i], dict) and candidate[i].get("role") == "system" for i in indices):
                record = self.receipt(candidate, indices)
                record["branch"] = job["branch"]
                self.records.append(record)
            return
        systems = [item for item in original if str(item.get("role", "")).lower() in {"system", "developer"}]
        index = len(systems)
        if not isinstance(candidate, list) or len(candidate) <= index or candidate[:index] != systems:
            return
        try:
            text = candidate[index]["content"]
            summary = json.loads(text.split("\n", 1)[1])
            if (not isinstance(summary, dict) or set(summary) != set(HANDOFF_KEYS)
                    or any(not isinstance(summary[k], list) for k in HANDOFF_KEYS)
                    or candidate[index] != _handoff_message(summary, self.api_kind)):
                return
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return
        record = self.receipt(candidate, [index])
        record["branch"] = job["branch"]
        self.records.append(record)


async def request_scope(current, *, owner, branch, api_kind, body, ancestors=()):
    scope = SummaryScope(owner=owner, branch=branch, api_kind=api_kind,
        cipher=current.compactor.cipher, store=current.store,
        ttl=int(current.settings.section("affinity").get("ttl_seconds", 86400)),
        ancestors=ancestors, protected=system_hashes(body, api_kind),
        source_messages=extract_messages(body, api_kind))
    await scope.load()
    return scope


async def recover_legacy(current, scope):
    """Bounded on-demand receipt recovery; no model calls or archive rewrites."""
    if scope is None:
        return
    from .compaction_jobs import CompactionJobs
    from .memory_service import _thread
    path = current.settings.runtime_path.with_name("compaction-jobs.sqlite3")
    if not path.is_file():
        return
    try:
        jobs = CompactionJobs(path, current.state_encryption_key, read_only=True)
        ready = await _thread(jobs.ready, scope.owner, scope.branch, ancestors=scope.branches[1:])
    except Exception as exc:
        # Recovery is optional proof, not permission to drop unknown messages.
        # Keep existing protection and never expose database contents in logs.
        log.warning("summary_legacy_recovery_unavailable error_type=%s", type(exc).__name__)
        return
    for job in ready:
        scope.adopt_legacy_job(job)
