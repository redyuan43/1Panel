"""Reproduce capsule accumulation with fixed summaries, no model/network calls.

A rejected round is a diagnostic finding, not a successful product acceptance.
The 4K window is a small synthetic structural probe, not a production capacity
claim or a prediction of the round on which a real conversation will fail.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_router.compaction import ContextCompactor, CapsuleCipher
from ai_router.errors import CompactionUnavailableError
from ai_router.summary_provenance import SummaryScope, system_hashes


class Counter:
    def count_request(self, body, kind):
        return len(json.dumps(body)) // 4 + 1


async def probe():
    compactor = ContextCompactor(Counter(), CapsuleCipher(Fernet.generate_key().decode()),
        internal_base_url="http://unused.invalid", internal_api_key="", model_id="fixed-summary",
        client=AsyncMock())
    compactor._summarize = AsyncMock(return_value={"facts": [
        "synthetic durable fact " + str(i) for i in range(20)]})
    original_rule = {"role": "system", "content": "User rules must remain unchanged."}
    messages = [original_rule]
    scope = SummaryScope(owner="synthetic", branch="probe", api_kind="chat", cipher=compactor.cipher,
                         protected=system_hashes({"messages": messages}, "chat"))
    report = {"model_calls": 0, "window": 4000, "state": "not_reproduced", "rounds": []}
    for number in range(1, 41):
        source = [*messages, {"role": "user", "content": "new synthetic evidence " * 2000},
            *[{"role": "user", "content": f"recent {number}-{i}"} for i in range(4)]]
        try:
            capsule = await compactor.compact({"messages": source}, api_kind="chat",
                target_context_tokens=4000, summary_input_tokens=100000, summary_scope=scope)
        except CompactionUnavailableError as exc:
            report.update(state="reproduced_rejection", rejected_round=number, reason=str(exc))
            break
        messages = compactor.cipher.decrypt(capsule.encrypted_messages)
        assert messages[0] == original_rule
        assert sum(item.get("role") == "system" for item in messages) == 2
        report["rounds"].append({"round": number, "after_tokens": capsule.after_tokens,
            "system_messages": sum(item.get("role") == "system" for item in messages)})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        parser.error("output must be a new absolute evidence path")
    result = asyncio.run(probe())
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result))
