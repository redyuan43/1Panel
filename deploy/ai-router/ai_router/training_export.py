from __future__ import annotations

import argparse
import asyncio
import os

from .training_archive import TrainingArchive


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export encrypted AI Router training records to JSONL",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--trainable-only", action="store_true")
    args = parser.parse_args()
    archive = TrainingArchive(
        os.environ.get(
            "AI_ROUTER_TRAINING_DB_PATH",
            "/training/conversations.sqlite3",
        ),
        os.environ.get(
            "AI_ROUTER_TRAINING_KEY_PATH",
            "/training/training.key",
        ),
    )
    count = asyncio.run(
        archive.export_jsonl(
            args.output,
            trainable_only=args.trainable_only,
        )
    )
    print(f"exported_records={count}")


if __name__ == "__main__":
    main()
