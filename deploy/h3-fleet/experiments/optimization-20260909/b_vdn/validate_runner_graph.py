"""B8 精确图校验；纯离线，返回隔离 runner 的 validate_workflow shape 合同。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


APPROVED_WORKFLOW = Path(__file__).with_name("workflow.json")
APPROVED_SHA256 = "7f51f086dae880f3e82f83cc6ce9e75349669f8b5e61397f785915cd74f27484"
MUTABLE_INPUTS = (("6", "prompt"), ("8", "noise_seed"), ("13", "filename_prefix"))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def relative_name(value):
    return (
        isinstance(value, str) and bool(value) and not value.startswith("/")
        and "\\" not in value and ":" not in value
        and not any(ord(character) < 32 for character in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def validate_workflow(graph, case=None):
    if case not in (None, "B8"):
        raise ValueError("B8 validator cannot authorize another recipe")
    raw = APPROVED_WORKFLOW.read_bytes()
    if hashlib.sha256(raw).hexdigest() != APPROVED_SHA256:
        raise ValueError("approved B8 workflow hash mismatch")
    approved = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(graph, dict) or set(graph) != set(approved):
        raise ValueError("B8 requires the exact approved node IDs and node count")
    candidate = copy.deepcopy(graph)
    try:
        for identifier, node in candidate.items():
            if not isinstance(identifier, str) or not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError("invalid API node")
        prompt = candidate["6"]["inputs"]["prompt"]
        seed = candidate["8"]["inputs"]["noise_seed"]
        prefix = candidate["13"]["inputs"]["filename_prefix"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("nonempty common prompt required; never rewritten")
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("seed must be uint64")
        if not relative_name(prefix):
            raise ValueError("unsafe output prefix")
        for identifier, name in MUTABLE_INPUTS:
            candidate[identifier]["inputs"][name] = approved[identifier]["inputs"][name]
        if canonical(candidate) != canonical(approved):
            raise ValueError("unaudited B8 node, wiring, field or parameter change")
    except (KeyError, TypeError) as error:
        raise ValueError("invalid B8 API workflow") from error
    return {"width": 480, "height": 864, "length": 362, "fps": 24,
            "steps": 8, "shift_video": 12, "shift_audio": 3, "save_node": "13"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--case", default="B8")
    args = parser.parse_args(argv)
    try:
        graph = json.loads(args.workflow.read_bytes(), object_pairs_hook=unique_object)
        shape = validate_workflow(graph, args.case)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(shape, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
