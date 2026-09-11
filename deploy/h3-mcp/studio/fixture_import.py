"""Offline administrator-only fixture import; never installed as an HTTP/MCP tool.

The caller supplies an existing isolated ConnectorAPI and its single Contract.
Importing this module does not load Studio, open a database, or inspect media.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import stat
import time
from pathlib import Path
from uuid import uuid4

from .connector_api import ConnectorError, IDENTIFIER, REVISION, _digest, _enabled, _operation_key, _validate


MAX_FIXTURE_BYTES = 512 * 1024 * 1024


def _write_gate():
    if not _enabled("H3_CONNECTOR_WRITES_ENABLED"):
        raise ConnectorError(403, "connector writes disabled")
    if _enabled("H3_CONNECTOR_GENERATION_ENABLED") or _enabled("AI_ROUTER_H3_MCP_GENERATION_ENABLED"):
        raise ConnectorError(403, "fixture import requires generation disabled")


def _pristine_draft(connector, project, expected_revision):
    connector.check_revision(project, {"expected_revision": expected_revision})
    connector.idle(project)
    if (project.get("prompt_processing") != "manual" or project.get("assets")
            or project.get("script_source") or project.get("stage_history")
            or project.get("connector_history") or project.get("connector_reviews")
            or project.get("connector_fixture_import")):
        raise ConnectorError(409, "fixture import requires a new acceptance draft without history")
    context = project["stages"]["context_ir"]
    outputs = project.get("router_outputs", {})
    output = outputs.get(context.get("output_id"), {})
    if (context["status"] != "approved" or not context.get("approved_at")
            or not project.get("prompt_approved") or project["prompt_approved"] != project["prompt_ir"]
            or output.get("stage") != "context_ir" or output.get("id") != context.get("output_id")
            or output.get("run_id") != context.get("run_id") or output.get("text") != project["prompt_approved"]
            or set(outputs) != {context.get("output_id")}):
        raise ConnectorError(409, "fixture import requires the confirmed immutable context output")
    for name, stage in project["stages"].items():
        if stage.get("cancel_requested") or stage.get("submission_unknown") or stage.get("batch_schedule_id"):
            raise ConnectorError(409, "fixture import requires an inactive unbatched draft")
        if name != "context_ir" and (stage["status"] != "pending" or any(stage.get(key) is not None for key in (
                "run_id", "output_id", "execution_id", "prompt_id", "execution", "artifact", "output_path",
                "workflow_path", "queued_at", "started_at", "finished_at", "approved_at"))):
            raise ConnectorError(409, "fixture import cannot replace an existing or previously executed stage")


def _project_directory(connector, task_id):
    expected = connector.module.SETTINGS.data_root.resolve() / "projects" / task_id
    directory = connector.module._project_dir(task_id)
    if directory != expected or not directory.is_dir() or directory.resolve() != expected:
        raise ConnectorError(409, "fixture target directory is not the owned project directory")
    outputs = directory / "router-outputs"
    if outputs.is_symlink() or outputs.resolve() != outputs:
        raise ConnectorError(409, "fixture output directory must not be redirected")
    return directory, outputs


def _copy_source(source, destination, expected_sha256, contract):
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ConnectorError(400, "fixture source must be an accessible regular local MP4 file") from error
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FIXTURE_BYTES:
        os.close(descriptor)
        raise ConnectorError(400, "fixture source must be a nonempty bounded regular file")
    with os.fdopen(descriptor, "rb") as incoming:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        contract.local.rollbacks.append(lambda: destination.unlink(missing_ok=True))
        digest, total = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_FIXTURE_BYTES:
                    raise ConnectorError(400, "fixture source exceeded the import size limit")
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
        if (digest.hexdigest() != expected_sha256 or total != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ConnectorError(409, "fixture source changed or did not match its authorized SHA-256")
        directory_descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return total


def import_fixture(connector, *, owner, task_id, expected_revision, operation_id,
                   source_path, source_sha256, authorization_reference):
    """Copy an explicitly authorized local fixture into one pristine owned draft.

    Only the provided Contract writes state and the idempotent receipt. No
    generator, startup handler, network call, decoder or source project is used.
    source_sha256 and authorization_reference are required administrator inputs;
    the latter is an approval record ID, not a credential or a filesystem path.
    """
    for field, value in (("owner", owner), ("task_id", task_id), ("operation_id", operation_id),
                         ("authorization_reference", authorization_reference)):
        _validate(value, IDENTIFIER, field)
    _validate(expected_revision, REVISION, "expected_revision")
    _validate(source_sha256, REVISION, "source_sha256")
    if not isinstance(source_path, (str, Path)):
        raise ConnectorError(400, "an explicit absolute local source path is required")
    source = Path(source_path)
    if not source.is_absolute() or source.suffix.lower() != ".mp4" or ".." in source.parts or "\x00" in str(source):
        raise ConnectorError(400, "an explicit absolute local MP4 source path is required")
    contract = connector.contract
    if contract.m is not connector.module or contract.store is not connector.store:
        raise ValueError("fixture import requires the existing Studio Contract")
    arguments = {"owner": owner, "task_id": task_id, "expected_revision": expected_revision,
                 "operation_id": operation_id, "source_path": str(source), "source_sha256": source_sha256,
                 "authorization_reference": authorization_reference}

    def save():
        _write_gate()
        project = connector.owned(task_id, owner)
        _pristine_draft(connector, project, expected_revision)
        directory, output_directory = _project_directory(connector, task_id)
        try:
            resolved_source = source.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ConnectorError(400, "fixture source is unavailable") from error
        if resolved_source != source or source.is_relative_to(directory):
            raise ConnectorError(400, "fixture source must be separate and must not use symlinks")
        output_directory.mkdir(exist_ok=True)
        output_id, run_id = "out_" + uuid4().hex, "fixture_run_" + uuid4().hex
        destination = output_directory / (output_id + ".mp4")
        size = _copy_source(source, destination, source_sha256, contract)
        _write_gate()
        metadata = {"origin": "fixture_import", "label": "导入验收视频，非本次生成",
                    "generated_in_this_task": False, "media_validation": "not_performed",
                    "source_sha256": source_sha256, "source_bytes": size,
                    "authorization_reference": authorization_reference,
                    "operation_id": operation_id, "imported_at": time.time(),
                    "context_output_id": project["stages"]["context_ir"]["output_id"],
                    "task_id": task_id, "run_id": run_id, "output_id": output_id}

        def attach(item):
            item["connector_fixture_import"] = {**metadata, "owner": owner, "source_path": str(source),
                                                "expected_revision": expected_revision}
            item["stages"]["preview"] = {
                **connector.module._new_stage(), "status": "awaiting_approval", "progress": 100,
                "detail": metadata["label"], "artifact": str(destination), "artifact_bytes": size,
                "run_id": run_id, "output_id": output_id, "fixture_import": metadata}
            item["router_outputs"][output_id] = {"id": output_id, "stage": "preview", "run_id": run_id,
                                                 "path": str(destination), "content_type": "video/mp4",
                                                 "fixture_import": metadata}

        return connector.public(contract.update(task_id, attach))

    with contract.connect():
        connector.owned(task_id, owner)
        result = contract.operation(_operation_key(owner, operation_id), _digest(["fixture_import", arguments]),
                                    task_id, save)
        connector.owned(result["id"], owner)
        return result


def existing_connector(module):
    """Obtain the installed Contract without another constructor or startup call."""
    from .connector_api import ConnectorAPI

    contract = getattr(module.STORE._connect, "__self__", None)
    if (contract is None or getattr(contract, "m", None) is not module
            or getattr(contract, "store", None) is not module.STORE):
        raise ValueError("Studio must already have its single Router Contract installed")
    return ConnectorAPI(module, contract)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline administrator-only H3 acceptance fixture import; no inference.")
    parser.add_argument("--execute", action="store_true",
                        help="Confirm this data directory is offline and the fixture import is explicitly authorized.")
    for name in ("data-root", "owner", "task-id", "expected-revision", "operation-id", "source-path",
                 "source-sha256", "authorization-reference"):
        parser.add_argument("--" + name, required=True)
    arguments = vars(parser.parse_args(argv))
    if not arguments.pop("execute"):
        parser.error("--execute is required; no Studio runtime or database was loaded")
    root = Path(arguments.pop("data_root"))
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        parser.error("--data-root must be an explicit existing offline directory without symlinks")
    database = root / "studio.sqlite3"
    if not database.is_file() or database.is_symlink():
        parser.error("the selected offline Studio database must already exist")
    _write_gate()
    os.environ["H3_STUDIO_DATA"] = str(root)
    module = importlib.import_module(__package__ + ".main")
    if module.SETTINGS.data_root != root or module.STORE.database_path != database:
        parser.error("the loaded Studio does not match the explicitly selected offline data directory")
    result = import_fixture(existing_connector(module), **arguments)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
