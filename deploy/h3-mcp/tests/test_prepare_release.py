from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("h3_prepare_release", ROOT / "scripts/prepare_release.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

CONTRACT = '''class Contract:
    def update(self, project_id, mutator):
        with self.lock:
            project = self.store.get(project_id)
            mutator(project)
            if project.get("router_managed"):
                self.record(project)
            return project


def install(module):
    contract = Contract(module)
    module.app.router.add_event_handler("startup", contract.initialize)
    api = APIRouter(prefix="/api/router")

    def managed(project_id):
        project = module._require_project(project_id)
        if not project.get("router_managed"):
            raise HTTPException(404, "router project not found")
        return project

    module.app.include_router(api)
    return contract
'''
ACCESS = '''def install_access(app):
    def secret():
        return "studio-secret"

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/api/"):
            key = secret()
            users = os.environ.get("H3_STUDIO_TAILSCALE_USERS", "")
            if request.headers.get("authorization") != "Bearer " + key:
                return JSONResponse({"detail": "studio authentication required"}, status_code=401)
        return await call_next(request)
'''
CONNECTOR = '''TOOL_DEFINITIONS = [{"name": "h3_capabilities", "inputSchema": {"type": "object"}}]

def connector_authorized(request):
    return request.headers.get("authorization") == "Bearer connector-secret"

def validate_connector_execution(project):
    if project.get("unsafe"):
        raise ValueError("execution binding changed")

def install_connector_api(module, contract):
    module.seen_contract = contract
    module.app.events.append("connector")
'''
CONTROL = '''def create_app():
    app = FastAPI()
    app.include_router(media_router(admin=True))
    app.mount("/", frontend)
    return app
'''
RECIPES = '''EXECUTION_FIELDS = ("recipe_id", "recipe_version", "backend_id", "runtime_version",
                    "gpu_uuid", "execution_seconds")


def execution_info(job):
    execution = job.get("execution") or {}
    contract = execution.get("contract") or {}
    return {key: source[key] for source in (contract, execution, job) for key in EXECUTION_FIELDS
            if source.get(key) is not None}
'''
CONSTRAINTS = (b"fastapi==0.124.4\nstarlette==0.50.0\npydantic==2.13.5\nanyio==4.15.1\n"
               b"httpx==0.28.1\nhttpcore==1.0.9\nmcp==1.26.0\nsse-starlette==3.4.11\n")
IMAGE = "sha256:" + "a" * 64


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


@pytest.fixture
def inputs(tmp_path):
    base = tmp_path / "live"
    put(base / "app/__init__.py", "")
    put(base / "app/main.py", 'LIVE = True\n')
    put(base / "app/router_contract.py", CONTRACT)
    put(base / "app/access.py", ACCESS)
    put(base / "app/recipes.py", RECIPES)
    put(base / "requirements.txt", "fastapi==0.124.4\n")
    put(base / "frontend/index.html", "<html>live</html>")
    put(base / "workflows/preview.json", "{}\n")
    put(base / "release-manifest.json", '{"previous": true}\n')
    connector = put(tmp_path / "overlay/connector_api.py", CONNECTOR)
    control = put(tmp_path / "extracted/control.py", CONTROL)
    mcp = put(tmp_path / "overlay/h3_mcp.py", "def install_h3_mcp(app):\n    pass\n")
    requirements = put(tmp_path / "overlay/requirements.txt", "".join(
        f"{name}=={version}\n" for name, version in prepare.MCP_PINS.items()))
    constraints = put(tmp_path / "operator/core.txt", CONSTRAINTS)
    return SimpleNamespace(base=base, connector=connector, control=control, mcp=mcp,
                           requirements=requirements, constraints=constraints, candidate=tmp_path / "candidate")


def studio(inputs, **overrides):
    return prepare.prepare_studio(**dict(live_source=inputs.base, candidate=inputs.candidate,
                                        connector_source=inputs.connector, **overrides))


def router(inputs, **overrides):
    arguments = dict(base_image=IMAGE, control_source=inputs.control, candidate=inputs.candidate,
                     mcp_source=inputs.mcp, connector_source=inputs.connector,
                     requirements=inputs.requirements, base_constraints=inputs.constraints)
    return prepare.prepare_router(**(arguments | overrides))


def test_studio_manifest_full_coverage_and_no_source_changes(inputs):
    before = {str(path): path.read_bytes() for path in inputs.base.rglob("*") if path.is_file()}
    manifest = studio(inputs)
    assert manifest["state"] == "candidate_only_not_deployed"
    files = {path.relative_to(inputs.candidate).as_posix(): path.read_bytes()
             for path in inputs.candidate.rglob("*") if path.is_file() and path.name != prepare.MANIFEST}
    assert manifest["files_sha256"] == {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    assert json.loads((inputs.candidate / prepare.MANIFEST).read_text()) == manifest
    assert files["app/connector_api.py"] == inputs.connector.read_bytes()
    assert files["app/connector_schema.json"] == prepare.connector_schema(inputs.connector.read_bytes())
    assert files["app/recipes.py"].replace(b', "admission_reason"', b"") == RECIPES.encode()
    assert manifest["base_provenance"]["files_sha256"]["app/recipes.py"] == prepare.digest(RECIPES.encode())
    assert files["release-manifest.json"] == (inputs.base / "release-manifest.json").read_bytes()
    assert all(Path(name).read_bytes() == value for name, value in before.items())
    assert manifest["base_provenance"]["source"] == str(inputs.base)
    assert manifest["base_provenance"]["files_sha256"]["app/router_contract.py"] == prepare.digest(CONTRACT.encode())
    assert "connector-secret" not in json.dumps(manifest)
    assert all(path.stat().st_mode & 0o222 == 0 for path in inputs.candidate.rglob("*") if path.is_file())


def test_explicit_fixture_cli_copies_only_selected_source_and_records_hash(inputs, capsys):
    fixture = put(inputs.connector.parent / "fixture_import.py", 'raise RuntimeError("must not execute")\n')
    put(fixture.parent / "unselected_helper.py", "invalid Python sibling must not be discovered")
    assert prepare.main(["studio", "--live-source", str(inputs.base), "--candidate", str(inputs.candidate),
                         "--connector-source", str(inputs.connector), "--fixture-source", str(fixture)]) == 0
    assert json.loads(capsys.readouterr().out)["deployed"] is False
    copied = inputs.candidate / "app/fixture_import.py"
    assert copied.read_bytes() == fixture.read_bytes()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o444
    assert not (inputs.candidate / "app/unselected_helper.py").exists()
    manifest = json.loads((inputs.candidate / prepare.MANIFEST).read_bytes())
    assert manifest["overlay"]["fixture_source"] == str(fixture)
    assert manifest["overlay"]["fixture_sha256"] == prepare.digest(fixture.read_bytes())
    expected = {path.relative_to(inputs.candidate).as_posix(): prepare.digest(path.read_bytes())
                for path in inputs.candidate.rglob("*") if path.is_file() and path.name != prepare.MANIFEST}
    assert manifest["files_sha256"] == expected


def test_fixture_is_not_copied_or_discovered_by_default(inputs):
    put(inputs.connector.parent / "fixture_import.py", "invalid optional Python must not be read")
    put(inputs.base / "app/fixture_import.py", "invalid inherited optional Python must not be read")
    manifest = studio(inputs)
    assert not (inputs.candidate / "app/fixture_import.py").exists()
    assert "fixture_source" not in manifest["overlay"]
    assert "fixture_sha256" not in manifest["overlay"]
    assert "app/fixture_import.py" not in manifest["files_sha256"]


def test_explicit_fixture_symlink_is_refused_before_output(inputs):
    fixture = inputs.connector.parent / "fixture_import.py"
    fixture.symlink_to(inputs.connector)
    with pytest.raises(prepare.PreparationError, match="symlink"):
        studio(inputs, fixture_source=fixture)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("name", [".env", ".git/HEAD", "app/__pycache__/main.pyc", "app/runtime.key",
                                 "app/secrets.json", "app/token.json", "app/data/state.json", "app/.env",
                                 "deploy/production.env", "app/state/session.json", "state/session.json",
                                 "logs/access.log", "app/runtime.sqlite3"])
def test_excludes_credentials_caches_and_runtime(inputs, name):
    put(inputs.base / name, "CREDENTIAL_SENTINEL")
    if name.startswith(".git/"):
        with pytest.raises(prepare.PreparationError, match="worktree"):
            studio(inputs)
        assert not inputs.candidate.exists()
    else:
        studio(inputs)
        assert not (inputs.candidate / name).exists()
        assert all(b"CREDENTIAL_SENTINEL" not in path.read_bytes()
                   for path in inputs.candidate.rglob("*") if path.is_file())


@pytest.mark.parametrize("mode", ["studio", "router"])
def test_existing_candidate_is_never_modified(inputs, mode):
    operation = studio if mode == "studio" else router
    operation(inputs)
    before = {str(path): path.read_bytes() for path in inputs.candidate.rglob("*") if path.is_file()}
    before_modes = {path: stat.S_IMODE(path.stat().st_mode)
                    for path in (inputs.candidate, *inputs.candidate.rglob("*"))}
    with pytest.raises(prepare.PreparationError, match="already exists"):
        operation(inputs)
    assert all(Path(name).read_bytes() == value for name, value in before.items())
    assert all(stat.S_IMODE(path.stat().st_mode) == mode for path, mode in before_modes.items())


@pytest.mark.parametrize("mask", [0o077, 0o777, 0o555, 0o444])
@pytest.mark.parametrize("kind", ["studio", "router"])
def test_candidate_permissions_ignore_child_umask_without_changing_inputs(inputs, tmp_path, mask, kind):
    existing = put(tmp_path / "existing-release/control.py", "preserve existing candidate\n")
    existing.chmod(0o400)
    existing.parent.chmod(0o700)
    before_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (tmp_path, *tmp_path.rglob("*"))}
    before_bytes = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    arguments = [kind, "--candidate", str(inputs.candidate), "--connector-source", str(inputs.connector)]
    if kind == "studio":
        arguments += ["--live-source", str(inputs.base)]
    else:
        arguments += ["--base-image", IMAGE, "--control-source", str(inputs.control),
                      "--mcp-source", str(inputs.mcp), "--requirements", str(inputs.requirements),
                      "--base-constraints", str(inputs.constraints)]
    child = '''import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("candidate_umask_test", sys.argv[1])
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)
os.umask(int(sys.argv[2], 8))

def forbid_umask_change(*args):
    raise AssertionError("preparer must not change process umask")

os.umask = forbid_umask_change
raise SystemExit(preparer.main(sys.argv[3:]))
'''
    result = subprocess.run([sys.executable, "-c", child, str(ROOT / "scripts/prepare_release.py"),
                             format(mask, "o"), *arguments], capture_output=True, text=True, timeout=20,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["deployed"] is False
    assert all(stat.S_IMODE(path.stat().st_mode) == mode for path, mode in before_modes.items())
    assert all(path.read_bytes() == content for path, content in before_bytes.items())
    for path in (inputs.candidate, *inputs.candidate.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o444)
    manifest = json.loads((inputs.candidate / prepare.MANIFEST).read_bytes())
    assert all(prepare.digest((inputs.candidate / name).read_bytes()) == checksum
               for name, checksum in manifest["files_sha256"].items())
    if kind == "router":
        assert (inputs.candidate / "Dockerfile").read_text().endswith("USER 10001:10001\n")


def test_writer_refuses_existing_directory_without_chmod(inputs):
    before = stat.S_IMODE(inputs.base.stat().st_mode)
    with pytest.raises(prepare.PreparationError, match="already exists"):
        prepare.write_candidate(inputs.base, {"new.py": b""}, {})
    assert stat.S_IMODE(inputs.base.stat().st_mode) == before
    assert not (inputs.base / "new.py").exists()


def test_writer_refuses_unowned_nested_directory_without_chmod(inputs, monkeypatch):
    original_mkdir = Path.mkdir

    def mkdir_with_collision(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == inputs.candidate:
            nested = path / "app"
            original_mkdir(nested, mode=0o700)
            nested.chmod(0o700)
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_collision)
    with pytest.raises(prepare.PreparationError, match="candidate incomplete"):
        prepare.write_candidate(inputs.candidate, {"app/new.py": b""}, {})
    assert stat.S_IMODE((inputs.candidate / "app").stat().st_mode) == 0o700
    assert not (inputs.candidate / "app/new.py").exists()
    assert not (inputs.candidate / prepare.MANIFEST).exists()


@pytest.mark.parametrize("change", [
    lambda source: source.replace("    module.app.include_router(api)\n", ""),
    lambda source: source.replace("    module.app.include_router(api)", "    module.app.include_router(api)\n    module.app.include_router(api)"),
    lambda source: source.replace("contract = Contract(module)", "contract = Contract(other)"),
    lambda source: source.replace("contract = Contract(module)", "contract = Contract(module)\n    another = Contract(module)"),
    lambda source: source.replace('    module.app.router.add_event_handler("startup", contract.initialize)\n', ""),
    lambda source: source.replace('if not project.get("router_managed"):', 'if True:'),
    lambda source: source.replace("module.app.include_router(api)", "module.app.include_router( api )"),
    lambda source: source.replace("module.app.include_router(api)", "module.app.include_router(api); dangerous()"),
    lambda source: source + "\n# module.app.include_router(api)\n",
])
def test_contract_hook_drift_fails_before_candidate_creation(inputs, change):
    put(inputs.base / "app/router_contract.py", change(CONTRACT))
    with pytest.raises(prepare.PreparationError, match="hook|Contract|managed"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_registration_reuses_one_contract_before_router_and_catchall(inputs, monkeypatch):
    from fastapi import HTTPException

    package = ModuleType("test_studio")
    connector = ModuleType("test_studio.connector_api")
    exec(CONNECTOR, connector.__dict__)
    monkeypatch.setitem(sys.modules, "test_studio", package)
    monkeypatch.setitem(sys.modules, "test_studio.connector_api", connector)
    calls = []
    instances = []

    class Contract:
        def __init__(self, module):
            instances.append(self)

        def initialize(self):
            pass

    app = SimpleNamespace(events=calls, router=SimpleNamespace(add_event_handler=lambda *args: calls.append("startup")),
                          include_router=lambda api: calls.append("router"))
    namespace = {"__package__": "test_studio", "Contract": Contract,
                 "APIRouter": lambda **kwargs: object(), "HTTPException": HTTPException}
    source = prepare.patch_contract(CONTRACT.encode()).decode()
    exec(source, namespace)
    namespace["Contract"] = Contract
    module = SimpleNamespace(app=app)
    returned = namespace["install"](module)
    calls.append("catchall")
    assert len(instances) == 1 and returned is module.seen_contract is instances[0]
    assert calls == ["startup", "connector", "router", "catchall"]
    assert source.index("install_connector_api(module, contract)") < source.index("module.app.include_router(api)")


@pytest.mark.parametrize("already_guarded", [False, True])
def test_managed_rejects_connector_owned_projects(already_guarded):
    import ast
    from fastapi import HTTPException

    source = CONTRACT.replace('if not project.get("router_managed"):',
                              'if not project.get("router_managed") or project.get("connector_owner"):') if already_guarded else CONTRACT
    patched = ast.parse(prepare.patch_contract(source.encode()))
    managed = next(node for node in ast.walk(patched) if isinstance(node, ast.FunctionDef) and node.name == "managed")
    project = {"router_managed": True}
    namespace = {"module": SimpleNamespace(_require_project=lambda project_id: project), "HTTPException": HTTPException}
    exec(compile(ast.Module(body=[managed], type_ignores=[]), "<managed>", "exec"), namespace)
    assert namespace["managed"]("legacy") is project
    project["connector_owner"] = "other-client"
    with pytest.raises(HTTPException) as failure:
        namespace["managed"]("owned")
    assert failure.value.status_code == 404


def test_execution_guard_exact_once_idempotent_and_after_mutation(monkeypatch):
    import contextlib

    connector = ModuleType("test_execution.connector_api")
    exec(CONNECTOR, connector.__dict__)
    monkeypatch.setitem(sys.modules, "test_execution", ModuleType("test_execution"))
    monkeypatch.setitem(sys.modules, "test_execution.connector_api", connector)
    patched = prepare.patch_execution_guard(CONTRACT.encode())
    assert prepare.patch_execution_guard(patched) == patched
    assert patched.count(b"validate_connector_execution(project)") == 1
    namespace = {"__package__": "test_execution"}
    exec(patched, namespace)
    contract = namespace["Contract"]()
    project = {"router_managed": True, "connector_owner": "client"}
    contract.lock = contextlib.nullcontext()
    contract.store = SimpleNamespace(get=lambda project_id: project)
    recorded = []
    contract.record = lambda value: recorded.append(dict(value))
    with pytest.raises(ValueError, match="binding changed"):
        contract.update("task", lambda value: value.update(unsafe=True))
    assert not recorded
    project.pop("connector_owner")
    assert contract.update("task", lambda value: None) is project
    assert len(recorded) == 1


@pytest.mark.parametrize("source", [
    CONTRACT.replace("            mutator(project)\n", ""),
    CONTRACT.replace("            mutator(project)", "            mutator(project)\n            mutator(project)"),
    CONTRACT.replace('if project.get("router_managed"):', 'if project.get("other"):'),
    CONTRACT.replace("            mutator(project)", "            mutator(project)\n            extra_call()"),
])
def test_execution_hook_drift_refused_before_candidate(inputs, source):
    put(inputs.base / "app/router_contract.py", source)
    with pytest.raises(prepare.PreparationError, match="execution|mutator"):
        studio(inputs)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("source", [RECIPES, RECIPES.replace('"execution_seconds")', '"execution_seconds",)')])
def test_recipes_patch_only_appends_reason_and_is_idempotent(source):
    patched = prepare.patch_recipes(source.encode())
    assert patched.replace(b', "admission_reason"', b"") == source.encode()
    assert prepare.patch_recipes(patched) == patched
    namespace = {}
    exec(patched, namespace)
    assert namespace["EXECUTION_FIELDS"] == prepare.EXPECTED_EXECUTION_FIELDS + ("admission_reason",)


@pytest.mark.parametrize("location", ["job", "execution", "contract"])
def test_candidate_recipes_preserves_fleet_waiting_reason(inputs, location):
    studio(inputs)
    namespace = {}
    exec((inputs.candidate / "app/recipes.py").read_bytes(), namespace)
    job = {"execution": {"contract": {}}, "gpu_uuid": "gpu-fixture", "execution_seconds": 0}
    target = job if location == "job" else job["execution"] if location == "execution" else job["execution"]["contract"]
    target["admission_reason"] = "waiting_for_capacity"
    result = namespace["execution_info"](job)
    assert result == {"gpu_uuid": "gpu-fixture", "execution_seconds": 0, "admission_reason": "waiting_for_capacity"}


@pytest.mark.parametrize("source", [
    RECIPES.replace("EXECUTION_FIELDS =", "OTHER_FIELDS ="),
    RECIPES + '\nEXECUTION_FIELDS = ("recipe_id",)\n',
    RECIPES + '\nEXECUTION_FIELDS += ("extra",)\n',
    RECIPES.replace('("recipe_id",', '["recipe_id",').replace('"execution_seconds")', '"execution_seconds"]'),
    RECIPES.replace('"gpu_uuid", "execution_seconds"', '"execution_seconds", "gpu_uuid"'),
    RECIPES.replace('"execution_seconds")', '"execution_seconds", "unexpected")'),
    RECIPES.replace('"execution_seconds")', '"execution_seconds", "admission_reason", "admission_reason")'),
    RECIPES.replace('"execution_seconds")', 'dynamic_field())'),
])
def test_recipes_hook_drift_refused_before_output(inputs, source):
    put(inputs.base / "app/recipes.py", source)
    with pytest.raises(prepare.PreparationError, match="EXECUTION_FIELDS"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_missing_recipes_fixture_refused_before_output(inputs, monkeypatch):
    real_walk = prepare.os.walk

    def without_recipes(*args, **kwargs):
        for directory, subdirectories, names in real_walk(*args, **kwargs):
            yield directory, subdirectories, [name for name in names if name != "recipes.py"]

    monkeypatch.setattr(prepare.os, "walk", without_recipes)
    with pytest.raises(prepare.PreparationError, match="missing app/recipes.py"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_help_documents_buildkit_override_without_running_build(capsys):
    with pytest.raises(SystemExit) as result:
        prepare.main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "DOCKER_BUILDKIT=0 docker build" in output
    assert "This preparer never builds" in output


def test_execution_export_missing_fails_closed(inputs):
    put(inputs.connector, CONNECTOR.replace("validate_connector_execution", "missing_execution_guard"))
    with pytest.raises(prepare.PreparationError, match="validate_connector_execution"):
        studio(inputs)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("path,credential,expected", [
    ("/api/router/connector/call/h3_capabilities", "connector-secret", 200),
    ("/api/router/connector/call/h3_capabilities", "studio-secret", 401),
    ("/api/router/connector/call/h3_capabilities", "", 401),
    ("/api/router/projects", "connector-secret", 401),
    ("/api/router/projects", "studio-secret", 200),
    ("/api/router/connector-evil/path", "connector-secret", 401),
    ("/api/router/connector", "connector-secret", 401),
    ("/api/projects", "studio-secret", 200),
])
def test_access_bypass_is_connector_only(monkeypatch, path, credential, expected):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.requests import Request
    import os

    connector = ModuleType("test_access.connector_api")
    exec(CONNECTOR, connector.__dict__)
    monkeypatch.setitem(sys.modules, "test_access", ModuleType("test_access"))
    monkeypatch.setitem(sys.modules, "test_access.connector_api", connector)
    namespace = {"__package__": "test_access", "os": os, "JSONResponse": JSONResponse}
    exec(prepare.patch_access(ACCESS.encode()), namespace)
    app = FastAPI()
    namespace["install_access"](app)
    middleware = app.user_middleware[0].kwargs["dispatch"]

    async def call_next(request):
        return JSONResponse({"ok": True})

    request = Request({"type": "http", "path": path,
                       "headers": [(b"authorization", ("Bearer " + credential).encode())]})
    assert asyncio.run(middleware(request, call_next)).status_code == expected


@pytest.mark.parametrize("target", ["file", "directory", "candidate", "source"])
def test_symlinks_are_refused(inputs, tmp_path, target):
    if target == "file":
        (inputs.base / "app/escape.py").symlink_to(inputs.connector)
    elif target == "directory":
        (inputs.base / "app/external").symlink_to(inputs.connector.parent, target_is_directory=True)
    elif target == "candidate":
        inputs.candidate.symlink_to(tmp_path / "not-created")
    else:
        alias = tmp_path / "base-alias"
        alias.symlink_to(inputs.base, target_is_directory=True)
        inputs.base = alias
    with pytest.raises(prepare.PreparationError, match="symlink"):
        studio(inputs)


def test_candidate_inside_base_refused(inputs):
    inputs.candidate = inputs.base / "new-candidate"
    with pytest.raises(prepare.PreparationError, match="outside"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_missing_auth_hook_fails_closed(inputs):
    put(inputs.connector, CONNECTOR.replace("connector_authorized", "missing_authorizer"))
    with pytest.raises(prepare.PreparationError, match="connector_authorized"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_access_hook_drift_fails_closed(inputs):
    put(inputs.base / "app/access.py", ACCESS.replace('startswith("/api/")', 'startswith("/api/router/")'))
    with pytest.raises(prepare.PreparationError, match="boundary"):
        studio(inputs)
    assert not inputs.candidate.exists()


def test_schema_deterministic_and_supplied_schema_checked(inputs, tmp_path):
    generated = prepare.connector_schema(inputs.connector.read_bytes())
    assert generated == prepare.connector_schema(inputs.connector.read_bytes())
    schema = put(tmp_path / "schema.json", json.dumps(json.loads(generated), indent=4))
    studio(inputs, schema=schema)
    assert (inputs.candidate / "app/connector_schema.json").read_bytes() == generated


def test_mismatched_schema_refused_before_output(inputs, tmp_path):
    schema = put(tmp_path / "schema.json", "[]")
    with pytest.raises(prepare.PreparationError, match="schema does not match"):
        studio(inputs, schema=schema)
    assert not inputs.candidate.exists()


def test_schema_does_not_execute_runtime_or_unsafe_declarations(inputs):
    runtime = CONNECTOR + '\nraise RuntimeError("runtime must not execute")\n'
    assert prepare.connector_schema(runtime.encode()) == prepare.connector_schema(CONNECTOR.encode())
    malicious = 'LEAK = open("/etc/passwd").read()\n' + CONNECTOR
    with pytest.raises(prepare.PreparationError, match="unsafe"):
        prepare.connector_schema(malicious.encode())


def test_router_context_is_narrow_digest_pinned_and_preserves_core(inputs):
    manifest = router(inputs)
    assert set(manifest["files_sha256"]) == {"Dockerfile", ".dockerignore", "ai_router/control.py",
            "ai_router/h3_mcp.py", "ai_router/h3_mcp_schema.json", "requirements-mcp.txt", "constraints-base.txt"}
    control = (inputs.candidate / "ai_router/control.py").read_text()
    assert control.replace("    from .h3_mcp import install_h3_mcp\n    install_h3_mcp(app)\n", "") == CONTROL
    assert control.index("install_h3_mcp(app)") < control.index('app.mount("/", frontend)')
    dockerfile = (inputs.candidate / "Dockerfile").read_text()
    assert dockerfile.startswith("FROM " + IMAGE + "\n")
    assert "COPY . " not in dockerfile and "COPY ai_router " not in dockerfile
    assert "-c /app/h3-mcp-constraints.txt" in dockerfile
    assert dockerfile.count("base core dependency drift") == 2
    assert dockerfile.endswith("USER 10001:10001\n")
    assert manifest["base_provenance"]["image_user"] == "10001:10001"
    assert manifest["mcp_dependencies"]["base_constraints"]["pydantic"] == "2.13.5"
    assert manifest["mcp_dependencies"]["base_constraints"]["mcp"] == "1.26.0"
    assert manifest["mcp_dependencies"]["base_constraints"]["sse-starlette"] == "3.4.11"
    assert manifest["base_provenance"]["control_sha256"] == prepare.digest(CONTROL.encode())
    assert inputs.control.read_text() == CONTROL
    assert (inputs.candidate / "ai_router/h3_mcp.py").read_bytes() == inputs.mcp.read_bytes()


def test_dependency_comments_are_not_copied_into_candidate(inputs):
    put(inputs.constraints, CONSTRAINTS + b"# private operator note must not be copied\n")
    router(inputs)
    assert all(b"private operator note" not in path.read_bytes()
               for path in inputs.candidate.rglob("*") if path.is_file())


@pytest.mark.parametrize("pin", [b"mcp==1.26.0\n", b"sse-starlette==3.4.11\n"])
def test_missing_base_sdk_pin_refuses_candidate(inputs, pin):
    put(inputs.constraints, CONSTRAINTS.replace(pin, b""))
    with pytest.raises(prepare.PreparationError, match="base constraints must pin"):
        router(inputs)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("before,after", [(b"mcp==1.26.0", b"mcp==2.2.0"),
                                        (b"sse-starlette==3.4.11", b"sse-starlette==3.0.3")])
def test_sdk_version_drift_refuses_candidate(inputs, before, after):
    put(inputs.requirements, inputs.requirements.read_bytes().replace(before, after))
    with pytest.raises(prepare.PreparationError, match="reviewed exact pins"):
        router(inputs)
    assert not inputs.candidate.exists()


def test_extracted_control_under_workspace_is_allowed(inputs, tmp_path):
    put(tmp_path / "extracted/.git", "gitdir: /operator/worktree-metadata\n")
    manifest = router(inputs)
    assert manifest["base_provenance"]["control_source"] == str(inputs.control)


def test_base_user_injection_is_refused(inputs):
    with pytest.raises(prepare.PreparationError, match="base user"):
        router(inputs, base_user="10001:10001\nRUN evil")
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("image", ["router:latest", "router:v1", "sha256:abc", IMAGE + "\nRUN evil", "https://user:secret@host/image"])
def test_mutable_or_unsafe_image_refused(inputs, image):
    with pytest.raises(prepare.PreparationError, match="immutable"):
        router(inputs, base_image=image)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("source", [CONTROL.replace("app.include_router(media_router(admin=True))", "pass"),
                                    CONTROL + "\nfrom .h3_mcp import install_h3_mcp\n",
                                    CONTROL.replace("    app.include_router(media_router(admin=True))",
                                        "    app.include_router(media_router(admin=True))\n    app.include_router(media_router(admin=True))")])
def test_router_missing_duplicate_or_dirty_hook_refused(inputs, source):
    put(inputs.control, source)
    with pytest.raises(prepare.PreparationError, match="hook"):
        router(inputs)
    assert not inputs.candidate.exists()


@pytest.mark.parametrize("content", [b"pydantic==2.13.5\n", CONSTRAINTS.replace(b"0.50.0", b"0.49.0"),
                                      CONSTRAINTS + b"--extra-index-url https://secret@example.com\n"])
def test_missing_conflicting_or_unsafe_constraints_refused(inputs, content):
    put(inputs.constraints, content)
    with pytest.raises(prepare.PreparationError, match="constraints|conflict"):
        router(inputs)
    assert not inputs.candidate.exists()


def test_cli_reports_preparation_only_and_clear_missing_hook(inputs, capsys):
    arguments = ["studio", "--live-source", str(inputs.base), "--connector-source", str(inputs.connector),
                 "--candidate", str(inputs.candidate)]
    put(inputs.base / "app/router_contract.py", CONTRACT.replace("contract = Contract(module)", "contract = None"))
    assert prepare.main(arguments) == 2
    assert "expected exactly once" in capsys.readouterr().err
    assert not inputs.candidate.exists()
    put(inputs.base / "app/router_contract.py", CONTRACT)
    assert prepare.main(arguments) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["deployed"] is False and output["built"] is False
    assert output["status"] == "candidate_only_not_deployed"
