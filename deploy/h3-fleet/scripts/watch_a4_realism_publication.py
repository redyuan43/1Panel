"""Watch A4_C05, A4_C1, A4_C0 folders in that order; never submit inference.

Example: --run-prefix RUN selects RUN-A4_C05, RUN-A4_C1, RUN-A4_C0.
Use three --case-folder CASE=FOLDER arguments for other directory conventions.
The timeout covers the whole watch, including transfers and publication, on Linux.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import publish_comparison_video as publisher


REMOTE_ROOT = "/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909"
CASES = ("A4_C05", "A4_C1", "A4_C0")
SUCCESS = "generated_pending_quality_review"
PENDING = {"prepared", "preflight", "acquiring_lease", "waiting_stable_idle_swap",
           "freeing_fast_cache", "submitting", "running", "queued"}
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
REMOTE_READER = """import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
folders=json.loads(sys.argv[2])
reports={}
for case,folder in folders.items():
    path=root/folder/'report.json'
    resolved=path.resolve()
    if resolved != root/folder/'report.json':
        raise ValueError('report escapes the specified folder')
    if (root/folder/'video.mp4').resolve() != root/folder/'video.mp4':
        raise ValueError('video escapes the specified folder')
    try:
        with path.open() as handle:
            reports[case]=json.load(handle)
    except FileNotFoundError:
        reports[case]=None
print(json.dumps(reports))
"""


def folder_mapping(prefix=None, entries=None):
    if prefix is None and entries is None:
        return {case: case for case in CASES}
    if bool(prefix) == bool(entries):
        raise ValueError("specify a run prefix OR all three CASE=FOLDER mappings")
    if prefix:
        entries = [f"{case}={prefix}-{case}" for case in CASES]
    mapping = {}
    for entry in entries:
        case, separator, folder = entry.partition("=")
        if not separator or case not in CASES or case in mapping:
            raise ValueError("only one mapping per A4_C05/A4_C1/A4_C0 is allowed")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", folder) or folder in {".", ".."}:
            raise ValueError("folder must be a safe single path component")
        mapping[case] = folder
    if set(mapping) != set(CASES) or len(set(mapping.values())) != 3:
        raise ValueError("all three cases require distinct folders")
    return {case: mapping[case] for case in CASES}


def ready(report, case, folder):
    if report is None:
        return False
    if not isinstance(report, dict) or report.get("case") != case:
        raise ValueError(f"{case}: report case mismatch")
    status = report.get("status")
    if status not in PENDING | {SUCCESS} or report.get("error") or report.get("reconciliation_error"):
        raise RuntimeError(f"{case}: terminal/invalid report: {json.dumps(report, ensure_ascii=False)}")
    if status != SUCCESS:
        return False
    if report.get("lease_released") is not True or report.get("isolated_unload_confirmed") is not True:
        return False
    expected = str(PurePosixPath(REMOTE_ROOT) / folder / "video.mp4")
    if report.get("artifact") != expected:
        raise ValueError(f"{case}: artifact is outside the fixed video.mp4 mapping")
    return True


def remaining(deadline, maximum):
    duration = deadline - time.monotonic()
    if duration <= 0:
        raise TimeoutError("publication watch timed out")
    return min(duration, maximum)


def poll(mapping, deadline):
    command = shlex.join(["python3", "-", REMOTE_ROOT, json.dumps(mapping)])
    result = subprocess.run(["ssh", *SSH_OPTIONS, "ivan", command], input=REMOTE_READER,
                            capture_output=True, text=True, check=True, timeout=remaining(deadline, 30))
    reports = json.loads(result.stdout)
    if not isinstance(reports, dict) or set(reports) != set(mapping):
        raise ValueError("remote report mapping mismatch")
    return reports


def collect(case, folder, observed, local_root, deadline):
    directory = local_root / folder
    directory.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="collection-", dir=directory))
    for name in ("report.json", "video.mp4"):
        source = f"ivan:{REMOTE_ROOT}/{folder}/{name}"
        subprocess.run(["scp", *SSH_OPTIONS, source, str(staging / name)],
                       capture_output=True, text=True, check=True, timeout=remaining(deadline, 300))
        if name == "report.json":
            downloaded = json.loads((staging / name).read_text())
            if not ready(downloaded, case, folder) or downloaded != observed:
                raise ValueError(f"{case}: report changed during collection")
    return staging / "report.json", staging / "video.mp4"


def record(local_root, event, **fields):
    payload = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": event, **fields}
    line = json.dumps(payload, ensure_ascii=False)
    with (local_root / "watch-a4-realism-publication.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def watch(mapping, local_root, destination, timeout=14400):
    local_root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    published = []
    try:
        if not (destination / "index.json").is_file():
            raise ValueError("destination must be the existing comparison-results gallery")
        record(local_root, "watching", folders=mapping, timeout=timeout)
        while len(published) < len(CASES):
            pending = {case: mapping[case] for case in CASES if case not in published}
            reports = poll(pending, deadline)
            for case, folder in pending.items():
                if not ready(reports[case], case, folder):
                    continue
                report_path, video = collect(case, folder, reports[case], local_root, deadline)
                remaining(deadline, 300)
                result = publisher.publish(report_path, video, destination)
                published.append(case)
                record(local_root, "published", case=case, report=str(report_path), result=result)
            if len(published) < len(CASES):
                time.sleep(remaining(deadline, 15))
        record(local_root, "completed", published=published)
        return published
    except (Exception, KeyboardInterrupt) as error:
        record(local_root, "failed", published=published, error=f"{type(error).__name__}: {error}",
               stderr=str(getattr(error, "stderr", "") or ""))
        raise


def persistent_path(value):
    path = Path(value).expanduser().resolve()
    if not Path(value).expanduser().is_absolute() or any(
        path == root or root in path.parents for root in map(Path, ("/tmp", "/var/tmp", "/dev/shm"))
    ):
        raise argparse.ArgumentTypeError("local root must be an absolute persistent path, not temporary storage")
    return path


def positive_timeout(value):
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return duration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-root", choices=[REMOTE_ROOT], default=REMOTE_ROOT)
    parser.add_argument("--local-root", type=persistent_path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--run-prefix")
    selection.add_argument("--case-folder", action="append", metavar="CASE=FOLDER")
    parser.add_argument("--timeout", type=positive_timeout, default=14400)
    args = parser.parse_args(argv)
    try:
        mapping = folder_mapping(args.run_prefix, args.case_folder)
    except ValueError as error:
        parser.error(str(error))

    def expire(signum, frame):
        raise TimeoutError("publication watch reached its total timeout")

    old_handler = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        watch(mapping, args.local_root, args.destination.resolve(), args.timeout)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"publication watch failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


if __name__ == "__main__":
    raise SystemExit(main())
