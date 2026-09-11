"""Prepare write-once candidates only: no build, deployment, keys or service calls.

Examples (all source paths are explicit; destinations must not already exist):
  python prepare_release.py studio --live-source /absolute/live/release \
    --connector-source /absolute/connector_api.py --candidate /workspace/new-studio
  python prepare_release.py router --base-image sha256:<64 hex characters> \
    --control-source /absolute/extracted-live/control.py \
    --mcp-source /absolute/h3_mcp.py --connector-source /absolute/connector_api.py \
    --requirements /absolute/requirements.txt --base-constraints /absolute/core.txt \
    --candidate /workspace/new-router

The operator must attest that the Studio directory and extracted Control source
are the current live bases. Git checkouts are rejected as Studio bases. No Docker calls
are made to discover or verify that attestation. An optional --schema must match
the connector module; otherwise canonical JSON is generated from its schema-only
declarations without importing or executing its runtime. Manifests are created
exclusively, last, and never updated. Their hashes cover every other output file,
including any original release manifest. This is a content-addressed preparation
boundary, not filesystem WORM protection or evidence of deployment.
Studio --fixture-source optionally freezes one administrator-only local import
module as app/fixture_import.py. It is never discovered or copied by default,
and preparation only parses its source; it does not import or execute it.

Build note for a separately approved operator step: on the current host, BuildKit
could not resolve the bare local FROM sha256:<image ID>. The operator verified
that DOCKER_BUILDKIT=0 docker build <candidate-directory> uses that exact local
image ID. Scope this setting to that one command; do not change global Docker
configuration or substitute a mutable image tag. This preparer never builds.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


MANIFEST = "candidate-manifest.json"
MCP_PINS = {"mcp": "1.26.0", "sse-starlette": "3.4.11",
            "starlette": "0.50.0", "fastapi": "0.124.4"}
CORE_PACKAGES = {"mcp", "sse-starlette", "fastapi", "starlette", "pydantic", "anyio", "httpx", "httpcore"}
EXPECTED_EXECUTION_FIELDS = ("recipe_id", "recipe_version", "backend_id", "runtime_version",
                             "gpu_uuid", "execution_seconds")
STUDIO_ROOT_FILES = {"README.md", "SOURCE.json", "requirements.txt", "release-manifest.json", "MANIFEST.sha256"}
STUDIO_SUFFIXES = {
    "app": {".py", ".json"}, "scripts": {".py"}, "workflows": {".json"},
    "frontend": {".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg",
                 ".webp", ".ico", ".woff", ".woff2", ".ttf"},
}
EXCLUDED = {"__pycache__", "node_modules", "venv", "state", "data", "logs", "secrets", "credentials"}


class PreparationError(ValueError):
    pass


def canonical_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def safe_path(value, *, exists=True):
    path = Path(os.path.abspath(value))
    if ".." in Path(value).parts:
        raise PreparationError("parent traversal is not allowed")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise PreparationError(f"symlink is not allowed: {component}")
    if exists and not path.exists():
        raise PreparationError(f"missing source: {path}")
    return path


def read_source(value):
    path = safe_path(value)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PreparationError(f"source must be a regular file: {path}")
        content = handle.read()
        after = os.fstat(handle.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise PreparationError(f"source changed while reading: {path}")
    return content


def live_base(value, *, directory=False):
    path = safe_path(value)
    if directory and not path.is_dir():
        raise PreparationError("live source must be a directory")
    for parent in ((path, *path.parents) if directory else path.parents):
        metadata = parent / ".git"
        if metadata.is_file() or (metadata / "HEAD").exists():
            raise PreparationError("live base must not be a Git development worktree")
    return path


def candidate_path(value, sources):
    candidate = safe_path(value, exists=False)
    if candidate.exists():
        raise PreparationError(f"candidate already exists; refusing to clobber: {candidate}")
    if not candidate.parent.is_dir():
        raise PreparationError("candidate parent must already exist")
    if any(candidate == source or candidate.is_relative_to(source) for source in sources):
        raise PreparationError("candidate must be outside all input source trees")
    return candidate


def parse_python(content, label):
    try:
        source = content.decode("utf-8") if isinstance(content, bytes) else content
        tree = ast.parse(source)
        compile(tree, label, "exec")
    except (SyntaxError, UnicodeError) as error:
        raise PreparationError(f"invalid Python source: {label}") from error
    return source, tree


def one_function(tree, name, parameters, *, asynchronous=False):
    matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == name]
    if len(matches) != 1:
        raise PreparationError(f"missing or ambiguous {name} hook; expected exactly once")
    function = matches[0]
    expected_type = ast.AsyncFunctionDef if asynchronous else ast.FunctionDef
    if (not isinstance(function, expected_type) or function.args.posonlyargs or function.args.kwonlyargs
            or function.args.vararg or function.args.kwarg or function.args.defaults
            or [argument.arg for argument in function.args.args] != parameters):
        raise PreparationError(f"drifted {name} signature")
    return function


def exact_statement(source, tree, marker, parent):
    expected = ast.dump(ast.parse(marker).body[0], include_attributes=False)
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)
               and ast.dump(node, include_attributes=False) == expected]
    if len(matches) != 1 or matches[0] not in parent.body or source.count(marker) != 1:
        raise PreparationError(f"missing, ambiguous or drifted hook; expected exactly once: {marker}")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    if node.lineno != node.end_lineno or lines[node.lineno - 1].strip() != marker:
        raise PreparationError(f"hook must occupy its own exact line: {marker}")
    return node


def insert_before(source, node, statements):
    lines = source.splitlines(keepends=True)
    indentation = " " * node.col_offset
    lines[node.lineno - 1:node.lineno - 1] = [indentation + line + "\n" for line in statements.splitlines()]
    return "".join(lines)


def patch_execution_guard(content):
    source, tree = parse_python(content, "router_contract.py execution guard")
    contracts = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Contract"]
    if len(contracts) != 1:
        raise PreparationError("missing or ambiguous Contract class execution hook")
    update = one_function(contracts[0], "update", ["self", "project_id", "mutator"])
    marker = ast.dump(ast.parse("mutator(project)").body[0])
    mutations = [node for node in ast.walk(update) if isinstance(node, ast.Expr) and ast.dump(node) == marker]
    if len(mutations) != 1 or source.count("mutator(project)") != 1:
        raise PreparationError("missing or ambiguous execution mutator hook; expected exactly once")
    mutation = mutations[0]
    containers = [body for node in ast.walk(update) for _, body in ast.iter_fields(node)
                  if isinstance(body, list) and mutation in body]
    if len(containers) != 1 or source.splitlines()[mutation.lineno - 1].strip() != "mutator(project)":
        raise PreparationError("drifted execution mutator hook")
    siblings = containers[0]
    position = siblings.index(mutation) + 1
    guard_source = '''if project.get("connector_owner"):
    from .connector_api import validate_connector_execution
    validate_connector_execution(project)'''
    guard = ast.parse(guard_source).body[0]
    installed = position < len(siblings) and ast.dump(siblings[position]) == ast.dump(guard)
    if installed:
        if source.count("validate_connector_execution") != 2:
            raise PreparationError("ambiguous existing connector execution guard")
        position += 1
    elif "validate_connector_execution" in source:
        raise PreparationError("drifted existing connector execution guard")
    expected = ast.parse('project.get("router_managed")', mode="eval").body
    if (position >= len(siblings) or not isinstance(siblings[position], ast.If)
            or ast.dump(siblings[position].test) != ast.dump(expected)
            or source.splitlines()[siblings[position].lineno - 1].strip() != 'if project.get("router_managed"):'):
        raise PreparationError("missing or drifted post-mutator router-managed execution hook")
    return (source if installed else insert_before(source, siblings[position], guard_source)).encode()


def patch_recipes(content):
    source, tree = parse_python(content, "recipes.py")
    bindings = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                and node.id == "EXECUTION_FIELDS" and isinstance(node.ctx, ast.Store)]
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "EXECUTION_FIELDS"]
    if len(bindings) != 1 or len(assignments) != 1 or not isinstance(assignments[0].value, ast.Tuple):
        raise PreparationError("missing, ambiguous or drifted EXECUTION_FIELDS tuple; expected exactly once")
    fields = assignments[0].value
    try:
        values = ast.literal_eval(fields)
    except (ValueError, TypeError) as error:
        raise PreparationError("drifted EXECUTION_FIELDS tuple") from error
    if values == EXPECTED_EXECUTION_FIELDS + ("admission_reason",):
        return source.encode()
    if values != EXPECTED_EXECUTION_FIELDS:
        raise PreparationError("drifted EXECUTION_FIELDS tuple")
    original = source.encode()
    last = fields.elts[-1]
    offset = sum(len(line) for line in original.splitlines(keepends=True)[:last.end_lineno - 1]) + last.end_col_offset
    patched = original[:offset] + b', "admission_reason"' + original[offset:]
    parse_python(patched, "patched recipes.py")
    return patched


def patch_contract(content):
    source, tree = parse_python(content, "router_contract.py")
    if "install_connector_api" in source:
        raise PreparationError("router_contract.py already has a connector hook")
    install = one_function(tree, "install", ["module"])
    contract = exact_statement(source, tree, "contract = Contract(module)", install)
    constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "Contract"]
    if len(constructors) != 1:
        raise PreparationError("expected exactly one existing Contract constructor")
    startup = exact_statement(source, tree,
                              'module.app.router.add_event_handler("startup", contract.initialize)', install)
    router = exact_statement(source, tree, "module.app.include_router(api)", install)
    if not contract.lineno < startup.lineno < router.lineno:
        raise PreparationError("drifted Contract startup/router order")
    managed = one_function(tree, "managed", ["project_id"])
    if managed not in install.body:
        raise PreparationError("managed hook must belong to install")
    legacy = ast.parse('''def managed(project_id):
    project = module._require_project(project_id)
    if not project.get("router_managed"):
        raise HTTPException(404, "router project not found")
    return project
''').body[0]
    guarded = ast.parse('''def managed(project_id):
    project = module._require_project(project_id)
    if not project.get("router_managed") or project.get("connector_owner"):
        raise HTTPException(404, "router project not found")
    return project
''').body[0]
    shape = ast.dump(managed, include_attributes=False)
    if shape not in {ast.dump(legacy), ast.dump(guarded)}:
        raise PreparationError("drifted managed ownership hook")
    source = insert_before(source, router,
                           "from .connector_api import install_connector_api\ninstall_connector_api(module, contract)")
    if shape == ast.dump(legacy):
        lines = source.splitlines(keepends=True)
        guard = managed.body[1]
        lines[guard.lineno - 1] = (' ' * guard.col_offset
                                 + 'if not project.get("router_managed") or project.get("connector_owner"):\n')
        source = "".join(lines)
    parse_python(source, "patched router_contract.py")
    return patch_execution_guard(source)


def patch_access(content):
    source, tree = parse_python(content, "access.py")
    if "connector_authorized" in source or "/api/router/connector" in source:
        raise PreparationError("access.py already has a connector hook")
    install = one_function(tree, "install_access", ["app"])
    protect = one_function(tree, "protect", ["request", "call_next"], asynchronous=True)
    if protect not in install.body or len(protect.decorator_list) != 1:
        raise PreparationError("drifted access middleware hook")
    if ast.dump(protect.decorator_list[0]) != ast.dump(ast.parse('app.middleware("http")', mode="eval").body):
        raise PreparationError("drifted access middleware registration")
    first = protect.body[0]
    expected = ast.parse('request.url.path.startswith("/api/")', mode="eval").body
    if (not isinstance(first, ast.If) or ast.dump(first.test) != ast.dump(expected)
            or ast.get_source_segment(source, first.body[0]) != "key = secret()"
            or "H3_STUDIO_TAILSCALE_USERS" not in ast.get_source_segment(source, first)):
        raise PreparationError("drifted access protection boundary")
    source = insert_before(source, first, '''if request.url.path.startswith("/api/router/connector/"):
    from .connector_api import connector_authorized
    if not connector_authorized(request):
        return JSONResponse({"detail": "connector authentication required"}, status_code=401)
    return await call_next(request)''')
    parse_python(source, "patched access.py")
    return source.encode()


def patch_control(content):
    source, tree = parse_python(content, "control.py")
    if "install_h3_mcp" in source or "from .h3_mcp" in source:
        raise PreparationError("control.py already has an H3 MCP hook; provide the live base")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_app"]
    if len(functions) != 1:
        raise PreparationError("missing or ambiguous create_app hook")
    marker = "app.include_router(media_router(admin=True))"
    router = exact_statement(source, tree, marker, functions[0])
    source = insert_before(source, router, "from .h3_mcp import install_h3_mcp\ninstall_h3_mcp(app)")
    parse_python(source, "patched control.py")
    return source.encode()


def connector_schema(content):
    source, tree = parse_python(content, "connector_api.py")
    for name, parameters in (("install_connector_api", ["module", "contract"]),
                             ("connector_authorized", ["request"]),
                             ("validate_connector_execution", ["project"])):
        if one_function(tree, name, parameters) not in tree.body:
            raise PreparationError(f"missing top-level connector export: {name}")
    builtins = {"list": list, "tuple": tuple, "dict": dict, "str": str,
                "int": int, "bool": bool, "isinstance": isinstance}
    declarations = []
    export = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_tool":
            if node.decorator_list or len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
                raise PreparationError("schema helper must be a pure return expression")
            declarations.append(node)
        elif isinstance(node, ast.Assign):
            declarations.append(node)
            if any(isinstance(target, ast.Name) and target.id in {"TOOL_DEFINITIONS", "TOOLS"}
                   for target in node.targets):
                export = node.targets[0].id
                break
    if export is None:
        raise PreparationError("missing connector tool schema export")
    allowed = (ast.Module, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant, ast.Dict,
               ast.List, ast.Tuple, ast.Set, ast.Starred, ast.FunctionDef, ast.arguments, ast.arg,
               ast.Return, ast.Call, ast.keyword, ast.Attribute, ast.DictComp, ast.comprehension,
               ast.IfExp, ast.BinOp, ast.Pow, ast.Sub, ast.Add, ast.UnaryOp, ast.USub)
    schema_tree = ast.Module(body=declarations, type_ignores=[])
    for node in ast.walk(schema_tree):
        if not isinstance(node, allowed):
            raise PreparationError("unsupported executable schema declaration")
        if isinstance(node, ast.Attribute) and (node.attr != "items" or not isinstance(node.value, ast.Name)):
            raise PreparationError("unsafe schema attribute")
        if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name) and node.func.id in {*builtins, "_tool"}
                or isinstance(node.func, ast.Attribute) and node.func.attr == "items"):
            raise PreparationError("unsafe schema call")
    namespace = {"__builtins__": builtins}
    try:
        exec(compile(schema_tree, "<connector-schema-only>", "exec"), namespace)
        definitions = namespace[export]
        names = [entry["name"] for entry in definitions]
        if (not isinstance(definitions, list) or not definitions or len(set(names)) != len(names)
                or any(not isinstance(entry["inputSchema"], dict) for entry in definitions)
                or any(not isinstance(name, str) or not name.startswith("h3_") for name in names)):
            raise ValueError("invalid tool definitions")
        return canonical_json(definitions)
    except (KeyError, TypeError, ValueError, NameError, AttributeError) as error:
        raise PreparationError("invalid connector schema declarations") from error


def load_connector(connector_source, schema_source=None):
    connector = read_source(connector_source)
    schema = connector_schema(connector)
    provenance = {"connector_source": str(safe_path(connector_source)), "connector_sha256": digest(connector),
                  "schema_sha256": digest(schema), "schema_origin": "connector module declarations"}
    if schema_source is not None:
        supplied = read_source(schema_source)
        try:
            if canonical_json(json.loads(supplied)) != schema:
                raise PreparationError("schema does not match connector module")
        except (ValueError, UnicodeError) as error:
            raise PreparationError("schema does not match connector module") from error
        provenance["schema_source_sha256"] = digest(supplied)
    return connector, schema, provenance


def excluded(path):
    return any(part.startswith(".") or part.lower() in EXCLUDED
               or part.lower().endswith((".env", ".pyc", ".pyo"))
               or re.search(r"(^|[_.-])(key|keys|secret|secrets|credential|credentials|token|tokens)([_.-]|$)",
                            part.lower()) for part in path.parts)


def studio_files(base):
    files = {}
    for root, directories, names in os.walk(base, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(base)
        selected = []
        for name in sorted(directories):
            relative = relative_root / name
            if excluded(relative) or relative.parts[0] not in STUDIO_SUFFIXES:
                continue
            safe_path(base / relative)
            selected.append(name)
        directories[:] = selected
        for name in sorted(names):
            relative = relative_root / name
            if excluded(relative) or relative.as_posix() == "app/fixture_import.py":
                continue
            allowed = (relative.as_posix() in STUDIO_ROOT_FILES if len(relative.parts) == 1
                       else relative.suffix.lower() in STUDIO_SUFFIXES.get(relative.parts[0], set()))
            if allowed:
                files[relative.as_posix()] = read_source(base / relative)
    for required in ("app/__init__.py", "app/main.py", "app/router_contract.py", "app/access.py",
                     "app/recipes.py", "requirements.txt"):
        if required not in files:
            raise PreparationError(f"live Studio base is missing {required}")
    if "app/connector_api.py" in files or "app/connector_schema.json" in files:
        raise PreparationError("live Studio base already contains the connector overlay")
    return files


def pinned_requirements(content, label):
    pins = {}
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise PreparationError(f"invalid {label}") from error
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([a-zA-Z0-9][a-zA-Z0-9_.-]*)==([0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9.+-]*)?)", line)
        if not match:
            raise PreparationError(f"{label} accepts exact package pins only")
        name = re.sub(r"[-_.]+", "-", match[1].lower())
        if name in pins:
            raise PreparationError(f"duplicate {label} pin: {name}")
        pins[name] = match[2]
    return pins


def write_candidate(candidate, files, metadata):
    metadata = {"format_version": 1, "state": "candidate_only_not_deployed", "built": False,
                "manifest_policy": "write-once; hashes cover all output files except this manifest",
                **metadata, "files_sha256": {name: digest(value) for name, value in sorted(files.items())}}
    if MANIFEST in files:
        raise PreparationError("candidate manifest output collision")
    for name in files:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise PreparationError("output is outside the candidate whitelist")
    manifest = canonical_json(metadata)
    try:
        candidate.mkdir(mode=0o755)
    except FileExistsError as error:
        raise PreparationError("candidate already exists; refusing to clobber") from error
    try:
        candidate.chmod(0o755, follow_symlinks=False)
        created_directories = {candidate}
        for name, content in [*sorted(files.items()), (MANIFEST, manifest)]:
            path = candidate / name
            directory = candidate
            for component in Path(name).parent.parts:
                directory = directory / component
                if directory not in created_directories:
                    directory.mkdir(mode=0o755)
                    directory.chmod(0o755, follow_symlinks=False)
                    created_directories.add(directory)
            safe_path(path, exists=False)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
    except OSError as error:
        raise PreparationError(f"candidate incomplete at {candidate}; not deployed; use a new destination") from error
    return metadata


def prepare_studio(*, live_source, candidate, connector_source, schema=None, fixture_source=None):
    base = live_base(live_source, directory=True)
    source_trees = [base, safe_path(connector_source).parent]
    if fixture_source is not None:
        fixture_source = safe_path(fixture_source)
        source_trees.append(fixture_source.parent)
    destination = candidate_path(candidate, source_trees)
    files = studio_files(base)
    base_hashes = {name: digest(value) for name, value in sorted(files.items())}
    connector, schema_bytes, overlay = load_connector(connector_source, schema)
    files["app/router_contract.py"] = patch_contract(files["app/router_contract.py"])
    files["app/access.py"] = patch_access(files["app/access.py"])
    files["app/recipes.py"] = patch_recipes(files["app/recipes.py"])
    files["app/connector_api.py"] = connector
    if b'_support("connector_assets")' in connector:
        for name in ("connector_assets.py", "input_contract.py"):
            content = read_source(safe_path(connector_source).with_name(name))
            parse_python(content, name)
            files["app/" + name] = content
    files["app/connector_schema.json"] = schema_bytes
    if fixture_source is not None:
        fixture = read_source(fixture_source)
        parse_python(fixture, "fixture_import.py")
        files["app/fixture_import.py"] = fixture
        overlay.update(fixture_source=str(fixture_source), fixture_sha256=digest(fixture))
    for name, content in files.items():
        if name.endswith(".py"):
            parse_python(content, name)
    return write_candidate(destination, files, {
        "kind": "studio", "base_provenance": {"source": str(base), "attestation": "operator-supplied current live source",
        "files_sha256": base_hashes, "tree_sha256": digest(canonical_json(base_hashes))},
        "overlay": overlay, "mcp_dependencies": {"installed_in_studio": False, "router_required_pins": MCP_PINS},
    })


def prepare_router(*, base_image, control_source, candidate, mcp_source, connector_source,
                   requirements, base_constraints, schema=None, base_user="10001:10001"):
    if not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[0-9a-f]{64}", base_image):
        raise PreparationError("base image must be an immutable sha256 digest, not a tag")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?::[a-zA-Z0-9_-]+)?", base_user):
        raise PreparationError("base user must be the exact inspected image USER")
    base = safe_path(control_source)
    destination = candidate_path(candidate, [base.parent, safe_path(mcp_source).parent,
                                             safe_path(connector_source).parent])
    control = read_source(base)
    mcp = read_source(mcp_source)
    parse_python(mcp, "h3_mcp.py")
    _, schema_bytes, overlay = load_connector(connector_source, schema)
    requirement_bytes = read_source(requirements)
    constraint_bytes = read_source(base_constraints)
    pins = pinned_requirements(requirement_bytes, "MCP requirements")
    constraints = pinned_requirements(constraint_bytes, "base constraints")
    if pins != MCP_PINS:
        raise PreparationError("MCP requirements must match the reviewed exact pins")
    if not CORE_PACKAGES.issubset(constraints):
        raise PreparationError("base constraints must pin " + ", ".join(sorted(CORE_PACKAGES)))
    if any(name in pins and pins[name] != version for name, version in constraints.items()):
        raise PreparationError("MCP requirements conflict with base core versions")
    files = {"ai_router/control.py": patch_control(control), "ai_router/h3_mcp.py": mcp,
             "ai_router/h3_mcp_schema.json": schema_bytes,
             "requirements-mcp.txt": "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())).encode(),
             "constraints-base.txt": "".join(f"{name}=={version}\n" for name, version in sorted(constraints.items())).encode()}
    management_copy = ""
    management_ignore = ""
    if b"from .h3_mcp_management import" in mcp:
        management = read_source(safe_path(mcp_source).with_name("h3_mcp_management.py"))
        parse_python(management, "h3_mcp_management.py")
        files["ai_router/h3_mcp_management.py"] = management
        management_copy = "COPY ai_router/h3_mcp_management.py /app/ai_router/h3_mcp_management.py\n"
        management_ignore = "!ai_router/h3_mcp_management.py\n"
    if b"from .h3_mcp_assets import" in mcp:
        content = read_source(safe_path(mcp_source).with_name("h3_mcp_assets.py"))
        parse_python(content, "h3_mcp_assets.py")
        files["ai_router/h3_mcp_assets.py"] = content
        management_copy += "COPY ai_router/h3_mcp_assets.py /app/ai_router/h3_mcp_assets.py\n"
        management_ignore += "!ai_router/h3_mcp_assets.py\n"
    verification = ("import importlib.metadata as metadata; from pathlib import Path; "
                    "pins=[line.split('#', 1)[0].strip() for line in "
                    "Path('/app/h3-mcp-constraints.txt').read_text().splitlines()]; "
                    "assert all(metadata.version(name)==version for name,version in "
                    "(pin.split('==') for pin in pins if pin)), 'base core dependency drift'")
    files["Dockerfile"] = (f"FROM {base_image}\n\nUSER root\n"
        "COPY ai_router/control.py /app/ai_router/control.py\n"
        "COPY ai_router/h3_mcp.py /app/ai_router/h3_mcp.py\n"
        f"{management_copy}"
        "COPY ai_router/h3_mcp_schema.json /app/ai_router/h3_mcp_schema.json\n"
        "COPY requirements-mcp.txt /app/h3-mcp-requirements.txt\n"
        "COPY constraints-base.txt /app/h3-mcp-constraints.txt\n"
        f'RUN python -c "{verification}" \\\n'
        "    && python -m pip install --no-cache-dir -r /app/h3-mcp-requirements.txt "
        "-c /app/h3-mcp-constraints.txt \\\n"
        f'    && python -c "{verification}" \\\n'
        f"    && python -m pip check\nUSER {base_user}\n").encode()
    files[".dockerignore"] = ("*\n!Dockerfile\n!requirements-mcp.txt\n!constraints-base.txt\n"
                              "!ai_router/\nai_router/*\n!ai_router/control.py\n!ai_router/h3_mcp.py\n"
                              f"!ai_router/h3_mcp_schema.json\n{management_ignore}").encode()
    return write_candidate(destination, files, {
        "kind": "router-build-context", "base_provenance": {"image_digest": base_image, "image_user": base_user,
        "control_source": str(base), "control_sha256": digest(control),
        "attestation": "operator-extracted current live image source; not inspected by this tool"},
        "overlay": {**overlay, "mcp_source": str(safe_path(mcp_source)), "mcp_sha256": digest(mcp)},
        "mcp_dependencies": {"pins": pins, "base_constraints": constraints,
                             "requirements_sha256": digest(requirement_bytes), "constraints_sha256": digest(constraint_bytes)},
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="kind", required=True)
    studio = modes.add_parser("studio", help="prepare a new Studio candidate only; not deployed")
    router = modes.add_parser("router", help="prepare a narrow Docker build context only; not built or deployed")
    for command in (studio, router):
        command.add_argument("--candidate", required=True, type=Path)
        command.add_argument("--connector-source", required=True, type=Path)
        command.add_argument("--schema", type=Path, help="optional schema to compare with module declarations")
    studio.add_argument("--live-source", required=True, type=Path)
    studio.add_argument("--fixture-source", type=Path,
                        help="optional administrator-only local import module; copied without execution")
    router.add_argument("--base-image", required=True)
    router.add_argument("--base-user", default="10001:10001", help="exact inspected Config.User; current live base: 10001:10001")
    for name in ("control-source", "mcp-source", "requirements", "base-constraints"):
        router.add_argument("--" + name, required=True, type=Path)
    arguments = vars(parser.parse_args(argv))
    kind = arguments.pop("kind")
    try:
        manifest = (prepare_studio if kind == "studio" else prepare_router)(**arguments)
    except (PreparationError, OSError) as error:
        print(f"candidate preparation refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"candidate": str(Path(arguments["candidate"]).absolute()),
                      "status": manifest["state"], "built": False, "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
