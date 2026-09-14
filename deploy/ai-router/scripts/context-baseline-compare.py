"""Read-only production-code comparison with an optimistic, zero-token summary.

No upstream/model calls. This proves a structural size limit, not semantic QA.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_router.token_counter import HuggingFaceTokenCounter
from cryptography.fernet import Fernet


async def compare(args):
    source = subprocess.check_output(["docker", "exec", args.container, "python", "-c",
        "import inspect; from pathlib import Path; import ai_router.compaction as c; print(Path(inspect.getfile(c)).read_text(), end='')"], text=True)
    module = types.ModuleType("ai_router._production_compaction_baseline")
    module.__package__ = "ai_router"
    sys.modules[module.__name__] = module
    exec(compile(source, "<read-only-production-compaction>", "exec"), module.__dict__)
    fixture = json.loads(args.fixture.read_text())
    counter = HuggingFaceTokenCounter(args.tokenizer)
    compactor = module.ContextCompactor(counter, module.CapsuleCipher(Fernet.generate_key().decode()),
        internal_base_url="http://invalid-no-network", internal_api_key="", model_id="baseline-oracle", client=AsyncMock())
    # Give the old algorithm an unrealistically tiny summary. If even that cannot
    # fit, no improvement to its summarization model alone can fix this fixture.
    compactor._summarize_bounded = AsyncMock(return_value={key: [] for key in module.HANDOFF_KEYS})
    body = fixture["body"]
    messages = module.extract_messages(body, "chat")
    _, recent = compactor._partition_recent([m for m in messages if m.get("role") not in {"system", "developer"}],
                                           272000, "chat")
    floor = counter.count_request({"messages": recent}, "chat")
    report = {"container": args.container, "production_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "fixture_sha256": fixture["body_sha256"], "input_tokens": counter.count_request(body, "chat"),
              "recent_only_tokens": floor, "target_window": 272000,
              "old_target_40_percent": 108800, "new_target_60_percent": 163200,
              "model_calls": 0, "comparison": "optimistic empty summary; structural bound only"}
    try:
        await compactor.compact(body, api_kind="chat", target_context_tokens=272000, summary_input_tokens=991808)
        report["state"] = "unexpectedly_accepted"
    except module.CompactionUnavailableError as exc:
        report.update(state="rejected", reason=str(exc))
    assert floor > 163200 and report["state"] == "rejected"
    with args.output.open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        parser.error("output must be an absolute new evidence file")
    asyncio.run(compare(args))
