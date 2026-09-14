"""Durable split backfill/live progress; no plaintext account IDs or content."""
from contextlib import closing
import math
import time


DEFAULTS = {"enabled": True, "batch_records": 8, "max_batch_seconds": 0.25,
            "duty_cycle": 0.2}


def indexing_options(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError("invalid compaction.history_indexing options")
    result = {**DEFAULTS, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("history indexing enabled must be boolean")
    if type(result["batch_records"]) is not int or not 1 <= result["batch_records"] <= 32:
        raise ValueError("history indexing batch_records must be 1..32")
    for name, low, high in (("max_batch_seconds", 0.01, 2), ("duty_cycle", 0.01, 0.5)):
        item = result[name]
        if type(item) not in (int, float) or not math.isfinite(item) or not low <= item <= high:
            raise ValueError(f"history indexing {name} must be {low}..{high}")
    return result


class IngestionProgress:
    def __init__(self, index):
        self.index = index

    def states(self, owners, head):
        result = {}
        with closing(self.index._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO memory_ingestion_meta VALUES('live_cursor',?)", (head,))
            live_frontier = db.execute("SELECT value FROM memory_ingestion_meta WHERE key='live_cursor'").fetchone()[0]
            if live_frontier > head:
                raise ValueError("archive head regressed; index reconciliation required")
            for owner in owners:
                digest = self.index._owner(owner)
                old = db.execute("SELECT cursor FROM memory_checkpoints WHERE owner=?", (digest,)).fetchone()
                cursor = min(old[0] if old else 0, head)
                db.execute("""INSERT OR IGNORE INTO memory_ingestion
                    (owner,backfill_cursor,boundary,live_cursor,observed_head)
                    VALUES(?,?,?,?,?)""", (digest, cursor, head, head, head))
                # Old workers may advance the contiguous cursor during rolling
                # replacement. Honor their verified work without losing holes.
                db.execute("""UPDATE memory_ingestion SET
                    backfill_cursor=MAX(backfill_cursor,MIN(?,boundary)),
                    live_cursor=MAX(live_cursor,?), observed_head=MAX(observed_head,?) WHERE owner=?""",
                    (cursor, cursor, head, digest))
                row = db.execute("SELECT * FROM memory_ingestion WHERE owner=?", (digest,)).fetchone()
                if row["live_cursor"] < live_frontier:
                    # A re-enabled account's missed live range becomes backfill,
                    # not a queue in front of everybody else's new records.
                    contiguous = row["backfill_cursor"] if row["backfill_cursor"] < row["boundary"] else row["live_cursor"]
                    db.execute("""UPDATE memory_ingestion SET backfill_cursor=?,boundary=?,live_cursor=?,
                        events_per_second=0,sample_at=0,sample_position=0
                        WHERE owner=?""", (contiguous, live_frontier, live_frontier, digest))
                result[owner] = dict(db.execute("SELECT * FROM memory_ingestion WHERE owner=?", (digest,)).fetchone())
                row = result[owner]
                contiguous = row["backfill_cursor"] if row["backfill_cursor"] < row["boundary"] else row["live_cursor"]
                db.execute("""INSERT INTO memory_checkpoints VALUES(?,?) ON CONFLICT(owner)
                    DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""", (digest, contiguous))
            if result:
                # Newly enabled accounts may start beyond the old shared tail;
                # move it only across a range no active account needs as live.
                db.execute("UPDATE memory_ingestion_meta SET value=MAX(value,?) WHERE key='live_cursor'",
                           (min(row["live_cursor"] for row in result.values()),))
        return result

    def advance(self, owners, lane, cursor, head):
        if lane not in {"backfill", "live"}:
            raise ValueError("invalid history lane")
        with closing(self.index._connect()) as db, db:
            for owner in owners:
                digest = self.index._owner(owner)
                row = db.execute("SELECT * FROM memory_ingestion WHERE owner=?", (digest,)).fetchone()
                back = max(row["backfill_cursor"], min(cursor, row["boundary"])) if lane == "backfill" else row["backfill_cursor"]
                live = max(row["live_cursor"], cursor) if lane == "live" else row["live_cursor"]
                db.execute("""UPDATE memory_ingestion SET backfill_cursor=?,live_cursor=?,
                    observed_head=MAX(observed_head,?) WHERE owner=?""",
                    (back, live, head, digest))
                contiguous = back if back < row["boundary"] else live
                db.execute("""INSERT INTO memory_checkpoints VALUES(?,?) ON CONFLICT(owner)
                    DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""", (digest, contiguous))
            if owners and lane == "live":
                db.execute("UPDATE memory_ingestion_meta SET value=MAX(value,?) WHERE key='live_cursor'", (cursor,))

    def sample(self, owners):
        """Sample once per complete cycle, including prior cooldown/contention."""
        now = time.time()
        with closing(self.index._connect()) as db, db:
            for owner in owners:
                digest = self.index._owner(owner)
                row = db.execute("SELECT * FROM memory_ingestion WHERE owner=?", (digest,)).fetchone()
                position = row["backfill_cursor"] + row["live_cursor"]
                moved, elapsed = position - row["sample_position"], now - row["sample_at"]
                if moved > 0:
                    speed = moved / elapsed if row["sample_at"] and elapsed > 0 else 0
                    db.execute("""UPDATE memory_ingestion SET events_per_second=?,sample_at=?,
                        sample_position=? WHERE owner=?""", (speed, now, position, digest))

    def status(self, owner):
        with closing(self.index._connect()) as db:
            # Read-only Control may briefly run against a pre-migration index.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_ingestion'").fetchone():
                return None
            row = db.execute("SELECT * FROM memory_ingestion WHERE owner=?", (self.index._owner(owner),)).fetchone()
        if row is None:
            return None
        remaining = row["boundary"] - row["backfill_cursor"] + row["observed_head"] - row["live_cursor"]
        speed = row["events_per_second"] if 0 <= time.time() - row["sample_at"] <= 60 else 0
        return {"backfill_cursor": row["backfill_cursor"], "backfill_boundary": row["boundary"],
                "live_cursor": row["live_cursor"], "observed_head": row["observed_head"],
                "remaining_events": remaining, "events_per_second": round(speed, 3),
                "estimated_remaining_seconds": round(remaining / speed) if speed > 0 else None,
                "caught_up": remaining == 0, "sample_at": row["sample_at"]}
