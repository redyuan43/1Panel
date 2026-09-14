#!/usr/bin/env python3
"""Offline reproducible r5-algorithm/shared-index comparison; synthetic data only.

The baseline reproduces per-account scans using the same current SQLite/index
implementation. Wall times are measurements, not production ETA promises.
"""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import random
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from ai_router.client_accounts import ClientAccountManager
from ai_router.content_audit import ArchiveReader
from ai_router.memory_index import MemoryIndex
from ai_router.memory_service import HistoryMemory
from ai_router.memory_sources import archived_sources
from ai_router.store import InMemoryStateStore
from ai_router.training_archive import TrainingArchive


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--records", type=int, default=128)
    args = parser.parse_args()
    assert 16 <= args.records <= 512
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    key_path = root / "synthetic.key"
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o600)
    writer = TrainingArchive(str(root / "synthetic.sqlite3"), str(key_path))
    state_key = Fernet.generate_key().decode()
    names = [f"owner-{i:02d}" for i in range(16)]
    rng = random.Random(20260913)
    try:
        for i in range(args.records):
            text = " ".join(f"token{rng.randrange(100000):05d}" for _ in range(512))
            await writer.begin(request_id=f"request-{i}", conversation_id=f"conversation-{i}",
                conversation_mode="stateful", client_id=names[i % 16], key_id="synthetic-key",
                protocol="chat", received_body={"messages": [{"role":"user", "content":text}]},
                instance_id="offline", boot_id="offline", history_source_local_only=False)
        reader = ArchiveReader(writer.database_path, key_path)
        baseline = MemoryIndex(root / "baseline.sqlite3", state_key)
        report = {"records":args.records, "accounts":16, "model_calls":0,
                  "baseline":"r5 per-account algorithm with common current index implementation",
                  "results":{}}
        wall, cpu = time.monotonic(), time.process_time()
        reads = 0
        for name in names:
            for _ in range(args.records):
                status = baseline.status(name)
                cursor, payloads = reader.history_page(name, status["archive_cursor"], limit=1)
                reads += 1
                for payload in payloads:
                    baseline.add(archived_sources(payload, client_id=name, cloud_allowed=True))
                baseline.checkpoint(name, cursor)
        report["results"]["per_account_unthrottled"] = {
            "seconds":time.monotonic()-wall,"cpu_seconds":time.process_time()-cpu,"decryptions":reads}
        def identities(index):
            with closing(index._connect()) as db:
                return set(tuple(row) for row in db.execute("SELECT owner,id FROM memory_documents"))
        expected = identities(baseline)
        for label, throttle in (("shared_unthrottled",False),("shared_default_throttle",True)):
            folder = root / label
            folder.mkdir()
            settings = SimpleNamespace(section=lambda _: {},runtime_path=folder / "settings.yaml")
            store = InMemoryStateStore()
            clients = ClientAccountManager(store, settings, state_key)
            for name in names:
                await clients.create_account(dict(id=name,name=name,models=["siyuan/auto"],
                    rpm_limit=10,tpm_limit=10000,max_parallel_requests=1),allowed_models={"siyuan/auto"})
            service = HistoryMemory(SimpleNamespace(training=writer,clients=clients,store=store,
                settings=settings,state_encryption_key=state_key,audit=Mock()))
            wall, cpu = time.monotonic(), time.process_time()
            reads = 0
            for _ in range(args.records + 1):
                result = await service.index_cycle(throttle=throttle)
                reads += result["read_events"]
                if result["state"] == "caught_up":
                    break
            else:
                raise AssertionError("index failed to catch up")
            assert identities(service.index) == expected
            assert reads == args.records
            report["results"][label] = {"seconds":time.monotonic()-wall,
                "cpu_seconds":time.process_time()-cpu,"decryptions":reads,"identical_sources":True}
        report["decryption_reduction_factor"] = 16
        report["speedup_unthrottled"] = report["results"]["per_account_unthrottled"]["seconds"] / report["results"]["shared_unthrottled"]["seconds"]
        (root / "report.json").write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2),flush=True)
    finally:
        writer.close()


if __name__ == "__main__":
    asyncio.run(main())
