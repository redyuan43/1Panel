from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml

from .config import Registry


def build_config(registry: Registry) -> dict[str, Any]:
    models = []
    for endpoint in registry.endpoints:
        if not endpoint.enabled:
            continue
        models.append(
            {
                "model_name": endpoint.id,
                "litellm_params": {
                    "model": f"openai/{endpoint.provider_model}",
                    "api_base": endpoint.api_base,
                    "api_key": f"os.environ/{endpoint.backend_api_key_env}",
                    "timeout": 900,
                    "stream_timeout": 900,
                },
                "model_info": {
                    "id": endpoint.id,
                    "base_model": endpoint.provider_model,
                    "mode": "chat",
                    "max_input_tokens": endpoint.safe_context_tokens,
                },
            }
        )
    return {
        "model_list": models,
        "general_settings": {
            "master_key": "os.environ/AI_ROUTER_LITELLM_MASTER_KEY",
            "disable_spend_logs": True,
        },
        "litellm_settings": {
            "drop_params": False,
            "request_timeout": 900,
            "set_verbose": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=os.environ.get(
            "AI_ROUTER_LITELLM_CONFIG_PATH",
            "/data/litellm.yaml",
        ),
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            build_config(Registry()),
            handle,
            allow_unicode=True,
            sort_keys=False,
        )
    os.replace(temporary, output)


if __name__ == "__main__":
    main()
