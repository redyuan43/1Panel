"""Keep the original u24 ninfer runtime behind the local handoff TCP gate."""
from __future__ import annotations

import json
import os
from pathlib import Path


ORIGINAL = Path("/home/ivan/.local/state/siyuan-media-validation/ninfer-restore.json")


def command(saved):
    argv = list(saved["argv"])
    for option in ("--device", "--host", "--port"):
        if argv.count(option) != 1 or argv.index(option) + 1 >= len(argv):
            raise RuntimeError("original 4060 Ti text command changed")
    if (not Path(argv[0]).is_file() or Path(argv[0]).name != "ninfer-serve"
            or argv[argv.index("--device") + 1] != "0"
            or argv[argv.index("--host") + 1] != "0.0.0.0"
            or argv[argv.index("--port") + 1] != "18086"):
        raise RuntimeError("original 4060 Ti text command changed")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if (args and Path(os.fsdecode(args[0])).name == "ninfer-serve"
                and b"--device" in args and args[args.index(b"--device") + 1:args.index(b"--device") + 2] == [b"0"]):
            raise RuntimeError("another 4060 Ti text process is still running")
    argv[argv.index("--host") + 1] = "127.0.0.1"
    argv[argv.index("--port") + 1] = "18087"
    return argv


def main():
    saved = json.loads(ORIGINAL.read_text())
    argv = command(saved)
    os.chdir(saved["cwd"])
    os.execvpe(argv[0], argv, saved["env"])


if __name__ == "__main__":
    main()
