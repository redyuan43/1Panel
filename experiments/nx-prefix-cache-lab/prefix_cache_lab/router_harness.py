from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys

from cryptography.fernet import Fernet
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
ROUTER_ROOT = REPOSITORY_ROOT / "deploy" / "ai-router"
if str(ROUTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ROUTER_ROOT))

from ai_router.api import create_app as create_router_app  # noqa: E402
from ai_router.config import Registry, Settings  # noqa: E402
from ai_router.runtime import build_runtime  # noqa: E402
from ai_router.store import InMemoryStateStore  # noqa: E402
from ai_router.token_counter import SimpleTokenCounter  # noqa: E402
from prefix_cache_lab.config import load_config  # noqa: E402
from prefix_cache_lab.remote import load_lab_api_key  # noqa: E402


CLIENT_KEY = "prefix-cache-lab-key"
ENDPOINT_ID = "nx3-qwen36-prefix-lab"


def _configure_environment(state_dir: Path) -> None:
    os.environ["AI_ROUTER_1PANEL_API_KEY"] = CLIENT_KEY
    os.environ["AI_ROUTER_LITELLM_MASTER_KEY"] = "unused-lab-key"
    os.environ["AI_ROUTER_STATE_KEY"] = Fernet.generate_key().decode("ascii")
    os.environ["AI_ROUTER_AUDIT_PATH"] = str(state_dir / "audit.jsonl")
    os.environ["AI_ROUTER_ROUTE_TRACE_DB_PATH"] = str(
        state_dir / "route-traces.sqlite3"
    )
    os.environ["AI_ROUTER_TRAINING_ENABLED"] = "false"
    os.environ.pop("AI_ROUTER_TRAINING_DB_PATH", None)
    os.environ.pop("AI_ROUTER_TRAINING_KEY_PATH", None)


def create_app():
    state_dir = ROOT / "private" / "router-harness"
    state_dir.mkdir(parents=True, exist_ok=True)
    _configure_environment(state_dir)

    base_registry = Registry(ROUTER_ROOT / "config" / "registry.yaml")
    endpoint = base_registry.by_id(ENDPOINT_ID)
    if endpoint is None:
        raise RuntimeError(f"missing Router endpoint: {ENDPOINT_ID}")
    config = load_config(ROOT / "config" / "nx2-nx4.yaml")
    api_key = load_lab_api_key(config.node("nx3"))
    os.environ[endpoint.backend_api_key_env] = api_key
    registry = base_registry.with_endpoints(
        [replace(endpoint, enabled=True, auto_candidate=False)]
    )
    settings = Settings(
        ROUTER_ROOT / "config" / "defaults.yaml",
        state_dir / "settings.yaml",
    )
    runtime = build_runtime(
        settings=settings,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
        instance_id="prefix-cache-lab",
    )
    return create_router_app(runtime)


def run(*, host: str, port: int) -> None:
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="info",
    )
