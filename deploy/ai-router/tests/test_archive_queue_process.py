"""Opt-in CPU integration: use ONLY a disposable Redis, never production."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

from cryptography.fernet import Fernet
import pytest
from redis import Redis

from ai_router.content_audit import ArchiveReader


@pytest.mark.skipif(not os.environ.get("ARCHIVE_TEST_REDIS_URL"), reason="requires disposable Redis")
@pytest.mark.parametrize("ack_mode", ["before_ack", "lost_ack_next", "lost_ack_last"])
def test_producer_exit_and_worker_kill_before_ack_are_recoverable(tmp_path, ack_mode):
    root = Path(__file__).resolve().parents[1]
    key = tmp_path / "training.key"
    key.write_bytes(Fernet.generate_key())
    db = tmp_path / "archive.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(root),
        "AI_ROUTER_TRAINING_DB_PATH":str(db), "AI_ROUTER_TRAINING_KEY_PATH":str(key),
        "AI_ROUTER_REDIS_URL":os.environ["ARCHIVE_TEST_REDIS_URL"],
        "AI_ROUTER_DEFAULTS_PATH":str(root / "config/defaults.yaml"),
        "AI_ROUTER_RUNTIME_SETTINGS_PATH":str(tmp_path / "settings.yaml"),
        "ACK_BARRIER":str(tmp_path / "ack-barrier"), "ACK_MODE":ack_mode}
    producer = r'''
import asyncio, os
from ai_router.archive_queue import QueuedTrainingArchive
async def main():
    a=QueuedTrainingArchive(os.environ['AI_ROUTER_TRAINING_DB_PATH'],os.environ['AI_ROUTER_TRAINING_KEY_PATH'],os.environ['AI_ROUTER_REDIS_URL'])
    token=await a.begin(request_id='process-restart',conversation_id=None,conversation_mode='stateless',client_id='test',key_id='test',protocol='chat',received_body={'messages':[]},instance_id='test',boot_id='old')
    await a.mark_routed(token,effective_body={},routed_body={},route={'selected_model':'test-model'})
    await a.complete(token,status_code=200,response_payload=b'{"answer":"ok"}')
    os._exit(0) # No Python shutdown hooks, memory flush or client close.
asyncio.run(main())
'''
    subprocess.run([sys.executable,"-c",producer],env=env,check=True,timeout=10)
    reader = ArchiveReader(str(db), str(key))
    assert reader.read("process-restart") is None
    first_worker = r'''
import asyncio, os
from pathlib import Path
from ai_router.archive_queue import ArchiveQueue
from redis.exceptions import TimeoutError
original=ArchiveQueue.acknowledge
original_failed=ArchiveQueue.failed
async def paused(self,token,encrypted):
    event=await self.decode(token,encrypted)
    target='complete' if os.environ['ACK_MODE']=='lost_ack_last' else 'mark_routed'
    if event['operation']==target:
        if os.environ['ACK_MODE']=='before_ack':
            Path(os.environ['ACK_BARRIER']).touch()
            await asyncio.Event().wait()
        await original(self,token,encrypted)
        raise TimeoutError('injected loss after real Redis ACK execution')
    return await original(self,token,encrypted)
async def failed(self,*args):
    await original_failed(self,*args)
    Path(os.environ['ACK_BARRIER']).touch()
    await asyncio.Event().wait()
ArchiveQueue.acknowledge=paused
ArchiveQueue.failed=failed
from ai_router.archive_worker import run
asyncio.run(run())
'''
    worker = subprocess.Popen([sys.executable,"-c",first_worker],env=env)
    def wait_for(check):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        raise AssertionError("subprocess checkpoint timed out")
    restarted = None
    redis = Redis.from_url(env["AI_ROUTER_REDIS_URL"])
    try:
        wait_for(lambda: Path(env["ACK_BARRIER"]).exists())
        if ack_mode != "before_ack":
            assert redis.hlen("router:archive:v1:retries") == 0
            assert redis.hlen("router:archive:v1:errors") == 0
        if ack_mode == "lost_ack_last":
            assert redis.zcard("router:archive:v1:ready") == 0
        worker.kill()
        worker.wait(timeout=5)
        assert len(reader.read("process-restart")["routing_attempts"]) == 1
        restarted = subprocess.Popen([sys.executable,"-m","ai_router.archive_worker"],env=env)
        wait_for(lambda: reader.read("process-restart")["state"] == "completed")
        wait_for(lambda: int(redis.get("router:archive:v1:bytes") or 0) == 0)
        assert redis.zcard("router:archive:v1:ready") == 0
        assert redis.zcard("router:archive:v1:oldest") == 0
        assert restarted.poll() is None
        value = reader.read("process-restart")
        assert len(value["routing_attempts"]) == 1
        assert value["response"]["body"]["value"] == {"answer":"ok"}
        # Prove the replacement process can consume new work, including when
        # the lost ACK already removed the previous request's final event.
        subprocess.run([sys.executable, "-c", producer.replace("process-restart", "after-restart")],
                       env=env, check=True, timeout=10)
        wait_for(lambda: (reader.read("after-restart") or {}).get("state") == "completed")
        wait_for(lambda: int(redis.get("router:archive:v1:bytes") or 0) == 0)
        assert redis.zcard("router:archive:v1:ready") == 0
        assert restarted.poll() is None
    finally:
        redis.close()
        for child in (worker,restarted):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
