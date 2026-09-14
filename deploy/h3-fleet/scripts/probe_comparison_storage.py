import argparse
import hashlib
import json
import mmap
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    data = b"H3-COMPARISON-STORAGE-PROBE\n" * 4096
    path = args.directory / "storage-probe.bin"
    if not path.exists():
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            if mapped[:] != data:
                raise RuntimeError("existing probe content differs or mmap read failed")
    mount = subprocess.check_output(["findmnt", "-J", "-T", str(args.directory), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], text=True)
    print(json.dumps({"path": str(path), "mmap_read": True, "sha256": hashlib.sha256(data).hexdigest(),
                      "free_bytes": shutil.disk_usage(args.directory).free, "mount": json.loads(mount)}))


if __name__ == "__main__":
    main()
