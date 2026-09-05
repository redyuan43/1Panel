from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NodeConfig:
    name: str
    ssh_host: str
    base_url: str
    provider_model: str | None = None
    production_base_url: str | None = None
    health_path: str = "/health"
    service_unit: str | None = None
    service_scope: str = "system"
    use_sudo: bool = False
    allow_lifecycle: bool = False
    lab_unit: str | None = None
    lab_prestart: tuple[tuple[str, ...], ...] = ()
    lab_environment: tuple[tuple[str, str], ...] = ()
    lab_command: tuple[str, ...] = ()
    slot_cache_file: str | None = None
    lab_api_key_file: str | None = None
    backend_api_key_env: str | None = None


@dataclass(frozen=True)
class RouterConfig:
    base_url: str
    model: str
    api_key_env: str


@dataclass(frozen=True)
class WorkBuddyConfig:
    ssh_host: str
    data_root: str
    traces_dir: str
    sessions_dir: str
    database: str


@dataclass(frozen=True)
class SamplingConfig:
    count: int = 8
    long_count: int = 6
    long_min_tokens: int = 40000
    long_max_tokens: int = 55000
    control_min_tokens: int = 20000
    control_max_tokens: int = 39999


@dataclass(frozen=True)
class LabConfig:
    path: Path
    nodes: dict[str, NodeConfig]
    router: RouterConfig
    workbuddy: WorkBuddyConfig
    sampling: SamplingConfig

    def node(self, name: str) -> NodeConfig:
        try:
            return self.nodes[name]
        except KeyError as exc:
            raise ValueError(f"unknown node: {name}") from exc


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _commands(value: Any, name: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    if value and all(not isinstance(item, list) for item in value):
        return (tuple(str(item) for item in value),)
    commands = []
    for index, command in enumerate(value):
        if not isinstance(command, list) or not command:
            raise ValueError(f"{name}[{index}] must be a non-empty list")
        commands.append(tuple(str(item) for item in command))
    return tuple(commands)


def load_config(path: Path) -> LabConfig:
    path = path.resolve()
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    root = _mapping(raw, "config")
    if int(root.get("version", 0)) != 1:
        raise ValueError("config version must be 1")

    nodes_raw = _mapping(root.get("nodes"), "nodes")
    nodes: dict[str, NodeConfig] = {}
    for name, value in nodes_raw.items():
        item = _mapping(value, f"nodes.{name}")
        nodes[name] = NodeConfig(
            name=name,
            ssh_host=str(item["ssh_host"]),
            base_url=str(item["base_url"]).rstrip("/"),
            provider_model=(
                str(item["provider_model"])
                if item.get("provider_model")
                else None
            ),
            production_base_url=(
                str(item["production_base_url"]).rstrip("/")
                if item.get("production_base_url")
                else None
            ),
            health_path=str(item.get("health_path", "/health")),
            service_unit=(
                str(item["service_unit"]) if item.get("service_unit") else None
            ),
            service_scope=str(item.get("service_scope", "system")),
            use_sudo=bool(item.get("use_sudo", False)),
            allow_lifecycle=bool(item.get("allow_lifecycle", False)),
            lab_unit=(str(item["lab_unit"]) if item.get("lab_unit") else None),
            lab_prestart=_commands(
                item.get("lab_prestart"),
                f"nodes.{name}.lab_prestart",
            ),
            lab_environment=tuple(
                (str(key), str(value))
                for key, value in _mapping(
                    item.get("lab_environment", {}),
                    f"nodes.{name}.lab_environment",
                ).items()
            ),
            lab_command=tuple(str(value) for value in item.get("lab_command", [])),
            slot_cache_file=(
                str(item["slot_cache_file"])
                if item.get("slot_cache_file")
                else None
            ),
            lab_api_key_file=(
                str(item["lab_api_key_file"])
                if item.get("lab_api_key_file")
                else None
            ),
            backend_api_key_env=(
                str(item["backend_api_key_env"])
                if item.get("backend_api_key_env")
                else None
            ),
        )

    router_raw = _mapping(root.get("router"), "router")
    router = RouterConfig(
        base_url=str(router_raw["base_url"]).rstrip("/"),
        model=str(router_raw["model"]),
        api_key_env=str(router_raw["api_key_env"]),
    )
    workbuddy_raw = _mapping(root.get("workbuddy"), "workbuddy")
    workbuddy = WorkBuddyConfig(
        ssh_host=str(workbuddy_raw["ssh_host"]),
        data_root=str(workbuddy_raw["data_root"]),
        traces_dir=str(workbuddy_raw["traces_dir"]),
        sessions_dir=str(workbuddy_raw["sessions_dir"]),
        database=str(workbuddy_raw["database"]),
    )
    sampling_raw = _mapping(root.get("sampling", {}), "sampling")
    sampling = SamplingConfig(
        count=int(sampling_raw.get("count", 8)),
        long_count=int(sampling_raw.get("long_count", 6)),
        long_min_tokens=int(sampling_raw.get("long_min_tokens", 40000)),
        long_max_tokens=int(sampling_raw.get("long_max_tokens", 55000)),
        control_min_tokens=int(sampling_raw.get("control_min_tokens", 20000)),
        control_max_tokens=int(sampling_raw.get("control_max_tokens", 39999)),
    )
    if not 1 <= sampling.long_count <= sampling.count:
        raise ValueError("sampling.long_count must be between 1 and count")
    return LabConfig(
        path=path,
        nodes=nodes,
        router=router,
        workbuddy=workbuddy,
        sampling=sampling,
    )
