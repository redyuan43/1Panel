from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vsa", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    sys.path.insert(0, str(runtime))
    sys.argv = ["cpu_runtime_audit", "--cpu", "--disable-api-nodes"]
    os.chdir(runtime)
    import comfy.options
    comfy.options.enable_args_parsing()
    import nodes
    import utils.extra_config
    utils.extra_config.load_extra_path_config(str(runtime / "extra_model_paths.yaml"))
    import_failed = asyncio.run(nodes.init_builtin_extra_nodes())
    loaded = {}
    for directory in ("h3_t8_baseline", "h3_b_vdn_minimal"):
        loaded[directory] = asyncio.run(nodes.load_custom_node(str(runtime / "custom_nodes" / directory)))
    required = ["LoraLoaderBypassModelOnly", "MiniMaxH3VDNRuntimeAuditT8Advanced",
                "MiniMaxH3VDNModelComposerT8Advanced", "MiniMaxH3VDNExecutionPlanT8Advanced"]
    if args.vsa:
        loaded["h3_d_vsa"] = asyncio.run(nodes.load_custom_node(str(runtime / "custom_nodes/h3_d_vsa")))
        required.extend(["D4StrictVSA", "D4FastH3Sigmas", "D4SamplingContract"])
    missing = [name for name in required if name not in nodes.NODE_CLASS_MAPPINGS]
    schemas = {name: nodes.NODE_CLASS_MAPPINGS[name].INPUT_TYPES() for name in required if name not in missing}
    result = {"status": "cpu_import_passed" if not missing and all(loaded.values()) else "failed",
              "runtime": str(runtime), "gpu_inference": False, "loaded": loaded,
              "missing_nodes": missing, "builtin_import_failures": import_failed,
              "schemas": schemas, "torch": importlib.metadata.version("torch"),
              "comfy_kitchen": importlib.metadata.version("comfy-kitchen")}
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if result["status"] != "cpu_import_passed":
        raise RuntimeError("required isolated nodes did not import")


if __name__ == "__main__":
    main()
