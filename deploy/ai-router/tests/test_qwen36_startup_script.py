from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-qwen36-shared-nx.sh"


def _fake_server(tmp_path: Path) -> tuple[Path, Path]:
    capture = tmp_path / "args.txt"
    server = tmp_path / "llama-server"
    server.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >\"$CAPTURE_PATH\"\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    return server, capture


def test_nx_startup_generates_and_requires_an_api_key(
    tmp_path: Path,
) -> None:
    server, capture = _fake_server(tmp_path)
    api_key = tmp_path / "config" / "api-key"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "SERVER_BIN": str(server),
        "CAPTURE_PATH": str(capture),
        "API_KEY_FILE": str(api_key),
    }

    subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert api_key.read_text(encoding="utf-8")
    assert stat.S_IMODE(api_key.stat().st_mode) == 0o600
    arguments = capture.read_text(encoding="utf-8").splitlines()
    position = arguments.index("--api-key-file")
    assert arguments[position + 1] == str(api_key)


def test_nx_startup_fails_closed_for_an_invalid_key_path(
    tmp_path: Path,
) -> None:
    server, capture = _fake_server(tmp_path)
    invalid_key = tmp_path / "api-key"
    invalid_key.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "SERVER_BIN": str(server),
        "CAPTURE_PATH": str(capture),
        "API_KEY_FILE": str(invalid_key),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not capture.exists()
