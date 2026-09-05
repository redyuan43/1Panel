from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import NodeConfig


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def run_ssh(host: str, command: str, *, timeout: int = 60) -> CommandResult:
    process = subprocess.run(
        ["ssh", host, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        stdout=process.stdout,
        stderr=process.stderr,
        returncode=process.returncode,
    )


def require_ssh(host: str, command: str, *, timeout: int = 60) -> str:
    result = run_ssh(host, command, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"ssh {host} failed: {detail or result.returncode}")
    return result.stdout


def run_remote_python(host: str, script: str, *, timeout: int = 300) -> Any:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        'python.exe -c "import base64;'
        f"exec(base64.b64decode('{encoded}').decode('utf-8'))"
        '"'
    )
    output = require_ssh(host, command, timeout=timeout)
    return json.loads(output)


def service_command(node: NodeConfig, action: str, *, apply: bool) -> list[str]:
    if action not in {"start", "stop", "restart"}:
        raise ValueError(f"unsupported service action: {action}")
    if not node.service_unit:
        raise ValueError(f"{node.name} has no service_unit")
    if not node.allow_lifecycle:
        raise ValueError(f"lifecycle changes are disabled for {node.name}")
    argv = ["systemctl"]
    if node.service_scope == "user":
        argv.append("--user")
    if node.use_sudo:
        argv = ["sudo", "-n", *argv]
    argv.extend([action, node.service_unit])
    if not apply:
        return argv
    require_ssh(node.ssh_host, " ".join(argv), timeout=180)
    return argv


def service_state(node: NodeConfig) -> str:
    if not node.service_unit:
        return "unconfigured"
    argv = ["systemctl"]
    if node.service_scope == "user":
        argv.append("--user")
    argv.extend(["is-active", node.service_unit])
    result = run_ssh(node.ssh_host, " ".join(argv), timeout=30)
    return (result.stdout.strip() or result.stderr.strip() or "unknown")


def user_service_state(node: NodeConfig) -> str:
    if not node.lab_unit:
        return "unconfigured"
    result = run_ssh(
        node.ssh_host,
        f"systemctl --user is-active {shlex.quote(node.lab_unit)}",
        timeout=30,
    )
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def lab_api_key_setup_command(node: NodeConfig) -> list[str]:
    if not node.lab_api_key_file:
        raise ValueError(f"{node.name} has no lab_api_key_file")
    script = (
        "from pathlib import Path; import os, secrets; "
        f"p=Path({node.lab_api_key_file!r}); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        "os.chmod(p.parent, 0o700); "
        "valid=p.is_file() and p.stat().st_size > 0; "
        "p.write_text(secrets.token_urlsafe(48) + '\\n', encoding='ascii') "
        "if not valid else None; "
        "os.chmod(p, 0o600)"
    )
    return ["python3", "-c", script]


def ensure_lab_api_key(node: NodeConfig, *, apply: bool) -> list[str]:
    command = lab_api_key_setup_command(node)
    if apply:
        require_ssh(node.ssh_host, shlex.join(command), timeout=60)
    return command


def load_lab_api_key(node: NodeConfig) -> str:
    if not node.backend_api_key_env:
        return ""
    configured = os.environ.get(node.backend_api_key_env, "").strip()
    if configured:
        return configured
    if not node.lab_api_key_file:
        raise ValueError(f"{node.name} has no lab_api_key_file")
    value = require_ssh(
        node.ssh_host,
        f"cat {shlex.quote(node.lab_api_key_file)}",
        timeout=30,
    ).strip()
    if not value or "\n" in value:
        raise RuntimeError(f"{node.name} lab API key file is invalid")
    os.environ[node.backend_api_key_env] = value
    return value


def start_lab_runtime(node: NodeConfig, *, apply: bool) -> list[list[str]]:
    if not node.allow_lifecycle:
        raise ValueError(f"lifecycle changes are disabled for {node.name}")
    if not node.lab_unit or not node.lab_command:
        raise ValueError(f"{node.name} has no lab runtime configuration")
    stop_production = service_command(node, "stop", apply=False)
    stop_lab = ["systemctl", "--user", "stop", node.lab_unit]
    key_setup = ensure_lab_api_key(node, apply=False)
    prestart = [list(command) for command in node.lab_prestart]
    run = [
        "systemd-run",
        "--user",
        f"--unit={node.lab_unit}",
        "--collect",
        "--property=Restart=no",
    ]
    for key, value in node.lab_environment:
        run.append(f"--setenv={key}={value}")
    run.extend(node.lab_command)
    commands = [key_setup, stop_production, stop_lab]
    commands.extend(prestart)
    commands.append(run)
    if not apply:
        return commands

    ensure_lab_api_key(node, apply=True)
    load_lab_api_key(node)
    service_command(node, "stop", apply=True)
    run_ssh(node.ssh_host, shlex.join(stop_lab), timeout=60)
    try:
        for command in prestart:
            require_ssh(node.ssh_host, shlex.join(command), timeout=60)
        require_ssh(node.ssh_host, shlex.join(run), timeout=180)
    except Exception:
        service_command(node, "start", apply=True)
        raise
    return commands


def restore_production_runtime(
    node: NodeConfig,
    *,
    apply: bool,
) -> list[list[str]]:
    if not node.allow_lifecycle:
        raise ValueError(f"lifecycle changes are disabled for {node.name}")
    if not node.lab_unit:
        raise ValueError(f"{node.name} has no lab runtime configuration")
    stop_lab = ["systemctl", "--user", "stop", node.lab_unit]
    start_production = service_command(node, "start", apply=False)
    commands = [stop_lab, start_production]
    if not apply:
        return commands
    run_ssh(node.ssh_host, shlex.join(stop_lab), timeout=60)
    service_command(node, "start", apply=True)
    return commands


def restart_lab_runtime(node: NodeConfig, *, apply: bool) -> list[str]:
    if not node.allow_lifecycle or not node.lab_unit:
        raise ValueError(f"lab runtime restart is disabled for {node.name}")
    command = ["systemctl", "--user", "restart", node.lab_unit]
    if apply:
        require_ssh(node.ssh_host, shlex.join(command), timeout=180)
    return command
