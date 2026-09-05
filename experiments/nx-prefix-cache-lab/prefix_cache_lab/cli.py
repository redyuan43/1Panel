from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
import random
import sys
from pathlib import Path

import httpx

from .benchmark import run_benchmark
from .config import LabConfig, load_config
from .remote import (
    load_lab_api_key,
    restore_production_runtime,
    service_state,
    start_lab_runtime,
    user_service_state,
)
from .report import collect_reports, render_markdown
from .sampler import (
    sample_candidates,
    sample_catalog,
    sample_quantile_candidates,
    select_profiled_cases,
    select_random_validation_cases,
)
from .synthetic import generate_synthetic_case, profile_cases
from .util import load_json, save_json
from .workbuddy_source import catalog_workbuddy, materialize_selection


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "nx2-nx4.yaml"


def _restore_and_verify(node) -> None:
    restore_production_runtime(node, apply=True)
    from .benchmark import _wait_health

    production_base_url = node.production_base_url or node.base_url
    _wait_health(production_base_url + node.health_path, timeout=600)
    if service_state(node) != "active":
        raise RuntimeError(f"{node.name} production service is not active")
    if user_service_state(node) == "active":
        raise RuntimeError(f"{node.name} lab service is still active")


def _start_runtime_checked(node, *, apply: bool) -> list[list[str]]:
    if not apply:
        return start_lab_runtime(node, apply=False)
    try:
        commands = start_lab_runtime(node, apply=True)
        from .benchmark import _wait_health

        _wait_health(
            node.base_url + node.health_path,
            timeout=600,
            api_key=load_lab_api_key(node),
        )
        return commands
    except Exception as primary:
        try:
            _restore_and_verify(node)
        except Exception as rollback:
            raise RuntimeError(
                f"lab runtime start failed: {type(primary).__name__}: {primary}; "
                "production rollback failed: "
                f"{type(rollback).__name__}: {rollback}"
            ) from primary
        raise RuntimeError(
            f"lab runtime start failed and production was restored: "
            f"{type(primary).__name__}: {primary}"
        ) from primary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate WorkBuddy prefix-cache behavior on Jetson NX workers."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect")

    for command in ("runtime-start", "runtime-restore"):
        runtime = subparsers.add_parser(command)
        runtime.add_argument("--node", default="nx3")
        runtime.add_argument("--apply", action="store_true")

    catalog = subparsers.add_parser("catalog-workbuddy")
    catalog.add_argument(
        "--output", type=Path, default=ROOT / "private" / "catalog.json"
    )

    sample = subparsers.add_parser("sample-workbuddy")
    sample.add_argument(
        "--catalog", type=Path, default=ROOT / "private" / "catalog.json"
    )
    sample.add_argument(
        "--output", type=Path, default=ROOT / "private" / "selection.json"
    )
    sample.add_argument("--seed", type=int)

    candidates = subparsers.add_parser("candidate-workbuddy")
    candidates.add_argument(
        "--catalog", type=Path, default=ROOT / "private" / "catalog.json"
    )
    candidates.add_argument(
        "--output",
        type=Path,
        default=ROOT / "private" / "candidate-selection.json",
    )
    candidates.add_argument("--seed", type=int)
    candidates.add_argument("--count", type=int, default=48)
    candidates.add_argument("--min-input-chars", type=int, default=90000)

    random_candidates = subparsers.add_parser("random-validation-candidates")
    random_candidates.add_argument(
        "--catalog", type=Path, default=ROOT / "private" / "catalog.json"
    )
    random_candidates.add_argument(
        "--output",
        type=Path,
        default=ROOT / "private" / "random-candidate-selection.json",
    )
    random_candidates.add_argument("--seed", type=int)
    random_candidates.add_argument("--count", type=int, default=96)
    random_candidates.add_argument("--quantiles", type=int, default=8)

    materialize = subparsers.add_parser("materialize-workbuddy")
    materialize.add_argument(
        "--selection", type=Path, default=ROOT / "private" / "selection.json"
    )
    materialize.add_argument(
        "--output-dir", type=Path, default=ROOT / "private" / "cases"
    )

    synthetic = subparsers.add_parser("generate-synthetic")
    synthetic.add_argument("--node", default="nx3")
    synthetic.add_argument("--target-tokens", type=int, default=45000)
    synthetic.add_argument("--seed", type=int, default=5603)
    synthetic.add_argument(
        "--output",
        type=Path,
        default=ROOT / "private" / "synthetic" / "synthetic-45k.json",
    )

    profile = subparsers.add_parser("profile-cases")
    profile.add_argument("--node", default="nx3")
    profile.add_argument(
        "--cases", type=Path, default=ROOT / "private" / "cases"
    )
    profile.add_argument(
        "--output", type=Path, default=ROOT / "private" / "case-profile.json"
    )

    final_sample = subparsers.add_parser("select-profiled-cases")
    final_sample.add_argument(
        "--profile",
        type=Path,
        default=ROOT / "private" / "candidate-profile.json",
    )
    final_sample.add_argument(
        "--output",
        type=Path,
        default=ROOT / "private" / "final-selection.json",
    )
    final_sample.add_argument("--seed", type=int)
    final_sample.add_argument("--case-id", action="append")

    random_sample = subparsers.add_parser("select-random-validation")
    random_sample.add_argument(
        "--profile",
        type=Path,
        default=ROOT / "private" / "random-candidate-profile.json",
    )
    random_sample.add_argument(
        "--output",
        type=Path,
        default=ROOT / "private" / "random-validation-selection.json",
    )
    random_sample.add_argument("--seed", type=int)

    for command in (
        "baseline",
        "warm",
        "verify",
        "interference-test",
        "restart-test",
        "output-isolation-test",
    ):
        item = subparsers.add_parser(command)
        item.add_argument("--transport", choices=("direct", "router"), default="direct")
        item.add_argument("--node", default="nx3")
        item.add_argument(
            "--cases", type=Path, default=ROOT / "private" / "cases"
        )
        item.add_argument(
            "--output-root", type=Path, default=ROOT / "artifacts"
        )
        item.add_argument("--seed", type=int, default=5603)
        item.add_argument("--runs", type=int, default=3)
        item.add_argument("--max-tokens", type=int, default=32)
        item.add_argument("--router-base-url")
        item.add_argument("--reset-slot", action="store_true")
        if command == "restart-test":
            item.add_argument("--apply", action="store_true")

    harness = subparsers.add_parser("router-harness")
    harness.add_argument("--host", default="127.0.0.1")
    harness.add_argument("--port", type=int, default=14081)

    report = subparsers.add_parser("report")
    report.add_argument(
        "--artifacts", type=Path, default=ROOT / "artifacts"
    )
    report.add_argument("--output", type=Path)
    return parser


def _inspect(config: LabConfig) -> dict:
    nodes = {}
    with httpx.Client(timeout=httpx.Timeout(5, connect=3)) as client:
        for name, node in config.nodes.items():
            try:
                response = client.get(node.base_url + node.health_path)
                health = {
                    "status_code": response.status_code,
                    "body": response.text[:500],
                }
            except Exception as exc:
                health = {"error": f"{type(exc).__name__}: {exc}"}
            nodes[name] = {
                "ssh_host": node.ssh_host,
                "base_url": node.base_url,
                "service_unit": node.service_unit,
                "service_state": service_state(node),
                "lab_service_state": user_service_state(node),
                "allow_lifecycle": node.allow_lifecycle,
                "health": health,
            }
    return {
        "config": str(config.path),
        "nodes": nodes,
        "router": {
            "base_url": config.router.base_url,
            "model": config.router.model,
            "api_key_configured": bool(os.environ.get(config.router.api_key_env)),
        },
        "workbuddy": {
            "ssh_host": config.workbuddy.ssh_host,
            "data_root": config.workbuddy.data_root,
        },
    }


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    if args.command == "inspect":
        print(json.dumps(_inspect(config), ensure_ascii=False, indent=2))
        return
    if args.command == "runtime-start":
        node = config.node(args.node)
        commands = _start_runtime_checked(node, apply=args.apply)
        print(
            json.dumps(
                {
                    "node": node.name,
                    "applied": args.apply,
                    "commands": commands,
                    "production_state": service_state(node),
                    "lab_state": user_service_state(node),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "runtime-restore":
        node = config.node(args.node)
        commands = restore_production_runtime(node, apply=False)
        if args.apply:
            _restore_and_verify(node)
        print(
            json.dumps(
                {
                    "node": node.name,
                    "applied": args.apply,
                    "commands": commands,
                    "production_state": service_state(node),
                    "lab_state": user_service_state(node),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "catalog-workbuddy":
        value = catalog_workbuddy(config.workbuddy)
        save_json(args.output, value)
        print(json.dumps({k: v for k, v in value.items() if k != "items"}, indent=2))
        return
    if args.command == "sample-workbuddy":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
        value = sample_catalog(
            load_json(args.catalog)["items"],
            config.sampling,
            seed=seed,
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "candidate-workbuddy":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
        value = sample_candidates(
            load_json(args.catalog)["items"],
            seed=seed,
            count=args.count,
            min_input_chars=args.min_input_chars,
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "random-validation-candidates":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
        value = sample_quantile_candidates(
            load_json(args.catalog)["items"],
            seed=seed,
            count=args.count,
            quantiles=args.quantiles,
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "materialize-workbuddy":
        value = materialize_selection(
            config.workbuddy,
            args.selection,
            args.output_dir,
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "generate-synthetic":
        value = generate_synthetic_case(
            config.node(args.node),
            target_tokens=args.target_tokens,
            output=args.output,
            seed=args.seed,
        )
        print(
            json.dumps(
                {key: value[key] for key in value if key != "prompt_text"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "profile-cases":
        from .benchmark import load_cases

        value = profile_cases(
            config.node(args.node),
            cases=load_cases(args.cases),
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "select-profiled-cases":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
        sampling = config.sampling
        value = select_profiled_cases(
            load_json(args.profile),
            seed=seed,
            count=sampling.count,
            long_count=sampling.long_count,
            long_min_tokens=sampling.long_min_tokens,
            long_max_tokens=sampling.long_max_tokens,
            control_min_tokens=sampling.control_min_tokens,
            control_max_tokens=sampling.control_max_tokens,
            case_ids=args.case_id,
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "select-random-validation":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
        value = select_random_validation_cases(
            load_json(args.profile),
            seed=seed,
        )
        save_json(args.output, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.command == "report":
        text = render_markdown(collect_reports(args.artifacts))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        print(text)
        return
    if args.command == "router-harness":
        from .router_harness import run

        run(host=args.host, port=args.port)
        return

    if args.router_base_url:
        config = replace(
            config,
            router=replace(
                config.router,
                base_url=args.router_base_url.rstrip("/"),
            ),
        )

    mode = {
        "interference-test": "interference",
        "restart-test": "restart",
        "output-isolation-test": "isolation",
    }.get(args.command, args.command)
    report = run_benchmark(
        config,
        mode=mode,
        transport=args.transport,
        node_name=args.node,
        cases_path=args.cases,
        output_root=args.output_root,
        seed=args.seed,
        runs=args.runs,
        max_tokens=args.max_tokens,
        apply=bool(getattr(args, "apply", False)),
        reset_slot=args.reset_slot,
    )
    print(json.dumps(report.get("summary") or report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
