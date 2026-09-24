#!/usr/bin/env python3
"""Explicit, reversible index preparation; defaults to read-only inspection."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_router.memory_index import SEARCH_INDEXES


def prepare(database, mode="inspect", timeout=120):
    if mode not in {"inspect", "apply", "rollback"}:
        raise ValueError("unsupported migration mode: " + str(mode))
    uri = Path(database).resolve().as_uri() + ("?mode=ro" if mode == "inspect" else "?mode=rw")
    db = sqlite3.connect(uri, uri=True, timeout=1)
    started = time.monotonic()
    names = [sql.split()[5] for sql in SEARCH_INDEXES]
    try:
        db.set_progress_handler(lambda: int(time.monotonic()-started >= timeout), 1000)
        if mode != "inspect":
            with db:
                db.execute("BEGIN IMMEDIATE")
                for name, sql in zip(names, SEARCH_INDEXES):
                    existing = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
                    if existing and existing[0] != sql.replace("IF NOT EXISTS ", ""):
                        raise ValueError("existing index definition differs: " + name)
                    db.execute(sql if mode == "apply" else "DROP INDEX IF EXISTS " + name)
        present = [name for name in names if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()]
        return {"mode": mode, "indexes": present, "ready": len(present) == len(names),
                "elapsed_seconds": round(time.monotonic()-started, 3)}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.database, "apply" if args.apply else "rollback" if args.rollback else "inspect")))
