"""Encrypted Redis outbox. Entries live until archive commit and acknowledgement.

Redis must outlive API containers. This does not promise power-loss durability.
Per-request lists preserve ordering, including across worker restarts/retries.
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from uuid import uuid4

from redis.asyncio import Redis
try:
    from asyncio import timeout
except ImportError:  # Python 3.10 development environments.
    from async_timeout import timeout

from .compute import BoundedExecutor
from .errors import TrainingArchiveUnavailableError
from .phase_timing import timed_async
from .training_archive import TrainingArchive


ENQUEUE = """
if redis.call('HEXISTS', KEYS[7], ARGV[6]) == 1 then return 2 end
if ARGV[5] == '1' and redis.call('GET', KEYS[8]) == 'blocked' then return -1 end
local size = string.len(ARGV[2])
for i = 7, #ARGV, 2 do
  if redis.call('HEXISTS', KEYS[6], ARGV[i]) == 0 then size = size + string.len(ARGV[i+1]) end
end
local used = tonumber(redis.call('GET', KEYS[3]) or '0')
if used + size > tonumber(ARGV[4]) then return 0 end
for i = 7, #ARGV, 2 do redis.call('HSETNX', KEYS[6], ARGV[i], ARGV[i+1]) end
redis.call('HSET', KEYS[7], ARGV[6], '1')
redis.call('EXPIRE', KEYS[7], 86400)
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('INCRBY', KEYS[3], size)
redis.call('ZADD', KEYS[4], 'NX', ARGV[3], ARGV[1])
if redis.call('HEXISTS', KEYS[5], ARGV[1]) == 0 then
  redis.call('ZADD', KEYS[2], 'NX', ARGV[3], ARGV[1])
end
return 1
"""

ACK = """
if redis.call('LINDEX', KEYS[1], 0) ~= ARGV[2] then return 0 end
redis.call('LPOP', KEYS[1])
redis.call('INCRBY', KEYS[3], -string.len(ARGV[2]))
redis.call('HDEL', KEYS[5], ARGV[1])
redis.call('HDEL', KEYS[8], ARGV[1])
if redis.call('LLEN', KEYS[1]) == 0 then
  local blobs = redis.call('HVALS', KEYS[6])
  local size = 0
  for _, blob in ipairs(blobs) do size = size + string.len(blob) end
  redis.call('INCRBY', KEYS[3], -size)
  redis.call('DEL', KEYS[6])
  redis.call('DEL', KEYS[1])
  redis.call('ZREM', KEYS[2], ARGV[1])
  redis.call('ZREM', KEYS[4], ARGV[1])
else
  redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
end
return 1
"""


RETRY_FAILED = """
-- An ACK may have committed even when its reply was lost. Never retry its successor.
if redis.call('LINDEX', KEYS[1], 0) ~= ARGV[2] then return 0 end
local count = redis.call('HINCRBY', KEYS[3], ARGV[1], 1)
redis.call('HSET', KEYS[4], ARGV[1], ARGV[3])
if count >= 5 and ARGV[4] == '1' then
  redis.call('HSET', KEYS[5], ARGV[1], ARGV[3])
  redis.call('ZREM', KEYS[2], ARGV[1])
else
  local delay = math.min(60, 2 ^ math.min(count, 6))
  redis.call('ZADD', KEYS[2], tonumber(ARGV[5]) + delay, ARGV[1])
end
return 1
"""

CLEAN_EMPTY_INDEX = """
-- Recheck atomically: a producer may have appended since the empty read.
if redis.call('LLEN', KEYS[1]) ~= 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
return 1
"""


def encode_event(event):
    # Only the response_payload field is bytes; user dictionaries are untouched.
    event = {**event, "kwargs": dict(event["kwargs"])}
    payload = event["kwargs"].get("response_payload")
    if isinstance(payload, bytes):
        event["kwargs"]["response_payload"] = base64.b64encode(payload).decode()
        event["response_base64"] = True
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()


def decode_event(value):
    event = json.loads(value)
    if event.get("response_base64"):
        event["kwargs"]["response_payload"] = base64.b64decode(
            event["kwargs"]["response_payload"], validate=True
        )
    return event


def pack_event(event, cipher):
    event = {**event, "kwargs": dict(event["kwargs"]), "body_refs": []}
    kwargs, blobs = event["kwargs"], {}

    def extract(container, key, path):
        raw = json.dumps(container[key], ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) < 65536:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in blobs:
            blobs[digest] = cipher.encrypt(raw)
        event["body_refs"].append([path + [key], digest])
        del container[key]

    for key in ("received_body", "effective_body", "routed_body"):
        if key in kwargs:
            extract(kwargs, key, [])
    if "pipeline" in kwargs:
        pipeline = kwargs["pipeline"] = dict(kwargs["pipeline"])
        bodies = pipeline["bodies"] = dict(pipeline.get("bodies", {}))
        for key in list(bodies):
            extract(bodies, key, ["pipeline", "bodies"])
    return cipher.encrypt(encode_event(event)), blobs


class ArchiveQueue:
    def __init__(self, redis, cipher, *, prefix="router:archive:v1", max_bytes=512 * 1024**2):
        self.redis, self.cipher, self.prefix = redis, cipher, prefix
        self.max_bytes = max_bytes
        self.encoder = BoundedExecutor(1, "archive-enqueue")

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    def keys(self, token):
        return [self.key("request:" + token), self.key("ready"), self.key("bytes"),
                self.key("oldest"), self.key("quarantine"), self.key("bodies:" + token),
                self.key("event-ids:" + token)]

    @timed_async("archive_enqueue")
    async def enqueue(self, operation, token, kwargs, *, event_id=None):
        event_id = event_id or uuid4().hex
        event = {"version": 1, "id": event_id, "operation": operation,
                 "token": token, "created_at": time.time(), "kwargs": kwargs}
        try:
            # Includes admission to the bounded serializer, not just network I/O.
            async with timeout(1):
                encrypted, blobs = await self.encoder.run(pack_event, event, self.cipher)
                blob_args = [value for pair in blobs.items() for value in pair]
                accepted = await self.redis.eval(ENQUEUE, 8, *self.keys(token), self.key("admission"), token,
                                                 encrypted, event["created_at"], self.max_bytes,
                                                 int(operation == "begin"), event_id, *blob_args)
                if accepted not in {1, 2}:
                    raise TrainingArchiveUnavailableError("archive queue is full or admission is blocked")
        except TrainingArchiveUnavailableError:
            raise
        except Exception as exc:
            raise TrainingArchiveUnavailableError("archive queue admission failed") from exc

    async def head(self):
        tokens = await self.redis.zrangebyscore(self.key("ready"), "-inf", time.time(), start=0, num=1)
        if not tokens:
            return None
        token = tokens[0].decode() if isinstance(tokens[0], bytes) else tokens[0]
        encrypted = await self.redis.lindex(self.keys(token)[0], 0)
        if encrypted is None:
            await self.redis.eval(CLEAN_EMPTY_INDEX, 5, self.keys(token)[0],
                                  self.key("ready"), self.key("oldest"),
                                  self.key("retries"), self.key("errors"), token)
            return None
        return token, encrypted

    async def acknowledge(self, token, encrypted):
        # The fifth ACK key is retries; quarantine is only changed by recovery.
        keys = self.keys(token)
        keys[4] = self.key("retries")
        return await self.redis.eval(ACK, 8, *keys, self.key("errors"), token, encrypted, time.time())

    async def decode(self, token, encrypted):
        event = await self.encoder.run(lambda: decode_event(self.cipher.decrypt(encrypted)))
        for path, digest in event.get("body_refs", []):
            blob = await self.redis.hget(self.key("bodies:" + token), digest)
            if blob is None:
                raise ValueError("archive body reference is missing")
            def unpack():
                raw = self.cipher.decrypt(blob)
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("archive body hash mismatch")
                return json.loads(raw)
            body = await self.encoder.run(unpack)
            container = event["kwargs"]
            for key in path[:-1]:
                container = container[key]
            container[path[-1]] = body
        return event

    async def failed(self, token, encrypted, error):
        from cryptography.fernet import InvalidToken
        poison = isinstance(error, (ValueError, TypeError, InvalidToken))
        return await self.redis.eval(
            RETRY_FAILED, 5, self.keys(token)[0], self.key("ready"),
            self.key("retries"), self.key("errors"), self.key("quarantine"),
            token, encrypted, type(error).__name__, int(poison), time.time(),
        )

    async def retry(self, token):
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hdel(self.key("quarantine"), token)
            pipe.hdel(self.key("retries"), token)
            pipe.hdel(self.key("errors"), token)
            pipe.zadd(self.key("ready"), {token: time.time()})
            await pipe.execute()

    async def status(self):
        oldest = await self.redis.zrange(self.key("oldest"), 0, 0, withscores=True)
        return {"mode": "redis", "pending_requests": await self.redis.zcard(self.key("oldest")),
                "pending_bytes": int(await self.redis.get(self.key("bytes")) or 0),
                "max_bytes": self.max_bytes,
                "oldest_age_seconds": max(0, time.time() - oldest[0][1]) if oldest else 0,
                "quarantined_requests": await self.redis.hlen(self.key("quarantine")),
                "failed_requests": await self.redis.hlen(self.key("errors")),
                "admission_blocked": await self.redis.get(self.key("admission")) == b"blocked",
                "worker_alive": bool(await self.redis.exists(self.key("worker-heartbeat")))}

    async def close(self):
        self.encoder.close()
        await self.redis.aclose()


class QueuedTrainingArchive(TrainingArchive):
    def __init__(self, database_path, key_path, redis_url):
        super().__init__(database_path, key_path)
        self.queue = ArchiveQueue(
            Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1), self._cipher,
            max_bytes=int(os.environ.get("AI_ROUTER_ARCHIVE_QUEUE_MAX_BYTES", 512 * 1024**2)),
        )

    async def begin(self, **kwargs):
        token = self._digest(f"request:{kwargs['request_id']}")
        await self.queue.enqueue("begin", token, kwargs)
        return token

    async def mark_routed(self, token, **kwargs):
        if token:
            await self.queue.enqueue("mark_routed", token, kwargs)

    async def set_effective_context(self, token, **kwargs):
        if token:
            await self.queue.enqueue("set_effective_context", token, kwargs)

    async def record_pipeline(self, token, pipeline):
        if token:
            await self.queue.enqueue("record_pipeline", token, {"pipeline": pipeline})

    async def complete(self, token, **kwargs):
        if token:
            await self.queue.enqueue("complete", token, kwargs)

    async def fail(self, token, **kwargs):
        if token:
            await self.queue.enqueue("fail", token, kwargs)

    async def publish_history(self, trace):
        token = self._digest(f"request:{trace['request_id']}")
        await self.queue.enqueue("history", token, {"trace": trace})

    async def enqueue_event(self, operation, token, kwargs, *, event_id):
        """Idempotently enqueue a process-local handoff retry."""
        if operation == "publish_history":
            operation = "history"
        await self.queue.enqueue(operation, token, kwargs, event_id=event_id)

    async def status(self):
        result = await super().status()
        result["queue"] = await self.queue.status()
        return result

    async def aclose(self):
        await self.queue.close()
        super().close()
