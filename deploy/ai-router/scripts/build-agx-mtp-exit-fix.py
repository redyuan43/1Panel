#!/usr/bin/env python3
"""Rebuild one patched server translation unit in a private copy of the AGX tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess


SOURCE_HASH = "275e6515ef2394d5fcca2fa560db12509b1af0b3438be3a4ec73ee63a204a9d1"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/home/agx/llama.cpp-mtp")
    parser.add_argument("--output", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("requires --execute")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output == source or source in output.parents or output in source.parents:
        parser.error("build output must be separate from the existing runtime")
    patch = Path(args.patch).resolve()
    cpp = Path("tools/server/server-context.cpp")
    binary = Path("build-agx-cuda/bin/llama-server")
    if digest(source / cpp) != SOURCE_HASH:
        raise ValueError("source differs from audited teardown implementation")
    original = {str(path): digest(source / path) for path in (cpp, binary)}
    output.mkdir(parents=True, exist_ok=False)
    work = output / "source"
    print("Copying audited source and build products", flush=True)
    shutil.copytree(source, work, symlinks=True)
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                   cwd=work, check=True)
    old = (source / cpp).read_text()
    before = """        llama_init.reset();

        for (server_slot & slot : slots) {
            if (slot.can_speculate()) {
                slot.spec.reset();
            }
        }
"""
    after = """        for (server_slot & slot : slots) {
            if (slot.can_speculate()) {
                slot.spec.reset();
            }
        }

        llama_init.reset();
"""
    if old.count(before) != 1 or (work / cpp).read_text() != old.replace(before, after, 1):
        raise ValueError("patch changed more than the audited destroy order")
    directory = work / "build-agx-cuda/tools/server"
    flags = {}
    for line in (directory / "CMakeFiles/server-context.dir/flags.make").read_text().splitlines():
        key, separator, value = line.partition(" = ")
        if separator:
            flags[key] = shlex.split(value)

    def relocate(values):
        return [value.replace(str(source), str(work)) for value in values]

    obj = directory / "CMakeFiles/server-context.dir/server-context.cpp.o"
    commands = [
        ["/usr/bin/c++", *relocate(flags["CXX_DEFINES"] + flags["CXX_INCLUDES"] + flags["CXX_FLAGS"]),
         "-o", str(obj), "-c", str(work / cpp)],
        ["/usr/bin/ar", "rcs", "libserver-context.a", str(obj)],
        relocate(shlex.split((directory / "CMakeFiles/llama-server.dir/link.txt").read_text())),
    ]
    for command in commands:
        print(shlex.join(command), flush=True)
        subprocess.run(command, cwd=directory, check=True)
    if any(digest(source / Path(name)) != value for name, value in original.items()):
        raise ValueError("original source or binary changed during build")
    report = {
        "passed": True, "original": original, "patch_sha256": digest(patch),
        "binary": str(work / binary), "binary_sha256": digest(work / binary),
        "patched_source_sha256": digest(work / cpp),
        "commands": commands, "cuda_kernels_rebuilt": False,
        "runtime_validation": "not_yet_performed",
    }
    with (output / "build-report.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
