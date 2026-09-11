from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import socket
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit
from xml.etree import ElementTree

from jsonschema import Draft202012Validator, ValidationError
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "integrations/workbuddy/h3-studio"
DOCUMENTATION = ROOT / "docs/h3-workbuddy-connector.md"
SCHEMA_SOURCE = ROOT.parent / "h3-mcp/studio/connector_api.py"
PACKAGE_FILES = {
    "connector-meta.json",
    "mcp.json",
    "token-schema.json",
    "icon.svg",
    "skills/h3-studio/SKILL.md",
}
ENDPOINT = "https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3"
SOURCE = "siyuan-h3-studio"
TOKEN_KEY = "H3_ACCESS_TOKEN"
TOOL_NAMES = {
    "h3_capabilities",
    "h3_prompt_guidance",
    "h3_save_draft",
    "h3_confirm_prompt",
    "h3_start_preview",
    "h3_list_tasks",
    "h3_get_task",
    "h3_review_preview",
    "h3_cancel_task",
}
NONEMPTY_STRING = {"type": "string", "minLength": 1}
META_TEXT_FIELDS = (
    "name", "name_zh", "name_en", "description", "description_zh", "description_en",
)
META_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        *META_TEXT_FIELDS, "source", "type", "auth_mode", "version",
        "minWorkbuddyVersion", "examples_zh", "examples_en",
    ],
    "properties": {
        **{field: NONEMPTY_STRING for field in META_TEXT_FIELDS},
        "source": {"const": SOURCE},
        "type": {"const": "mcp"},
        "auth_mode": {"const": "token"},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "minWorkbuddyVersion": {"const": "4.24.0"},
        **{
            field: {
                "type": "array", "minItems": 2, "maxItems": 5,
                "uniqueItems": True, "items": NONEMPTY_STRING,
            }
            for field in ("examples_zh", "examples_en")
        },
    },
}
MCP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mcpServers"],
    "properties": {
        "mcpServers": {
            "type": "object",
            "additionalProperties": False,
            "required": [SOURCE],
            "properties": {
                SOURCE: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "url", "headers", "timeout"],
                    "properties": {
                        "type": {"const": "streamableHttp"},
                        "url": {"const": ENDPOINT},
                        "headers": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["Authorization"],
                            "properties": {
                                "Authorization": {"const": "Bearer ${H3_ACCESS_TOKEN}"},
                            },
                        },
                        "timeout": {"type": "integer", "const": 30000},
                    },
                },
            },
        },
    },
}
TOKEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "fields"],
    "properties": {
        **{
            field: NONEMPTY_STRING
            for field in ("title", "title_en", "description", "description_en")
        },
        "fields": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "type", "required"],
                "properties": {
                    "key": {"const": TOKEN_KEY},
                    "type": {"const": "password"},
                    "required": {"const": True},
                    **{
                        field: NONEMPTY_STRING
                        for field in (
                            "label", "label_en", "placeholder",
                            "description", "description_en",
                        )
                    },
                },
            },
        },
    },
}
SCHEMAS = {
    "connector-meta.json": META_SCHEMA,
    "mcp.json": MCP_SCHEMA,
    "token-schema.json": TOKEN_SCHEMA,
}
SECRET_PATTERNS = (
    r"\bsk-[A-Za-z0-9_-]{16,}",
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    r"(?i)\bBearer\s+(?!\$\{)[A-Za-z0-9_./+=-]{12,}",
    r"(?i)\b(?:api_key|access_token|password|secret)\s*[=:]\s*[\"']?"
    r"[A-Za-z0-9][A-Za-z0-9_./+=-]{15,}",
)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def contains_secret(value):
    return any(re.search(pattern, value) for pattern in SECRET_PATTERNS)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Connector artifact tests must remain offline")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def artifacts():
    return {
        filename: json.loads(
            (PACKAGE / filename).read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
        for filename in SCHEMAS
    }


@pytest.fixture
def skill():
    return (PACKAGE / "skills/h3-studio/SKILL.md").read_text(encoding="utf-8")


@pytest.fixture
def studio_schema_module():
    specification = importlib.util.spec_from_file_location("h3_connector_schema_contract", SCHEMA_SOURCE)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def canonical_tools(studio_schema_module):
    module = studio_schema_module
    definitions = module.TOOL_DEFINITIONS
    assert isinstance(definitions, list)
    assert len(definitions) == len(TOOL_NAMES)
    assert {definition["name"] for definition in definitions} == TOOL_NAMES
    for definition in definitions:
        Draft202012Validator.check_schema(definition["inputSchema"])
    return {definition["name"]: definition for definition in definitions}


@pytest.fixture
def receipt_reader(studio_schema_module):
    module = studio_schema_module
    original = {
        "id": "fixture-task", "task_id": "fixture-task", "revision": "a" * 64,
        "context_output_id": "fixture-context", "status": "awaiting_prompt_approval",
    }
    current = {**original, "revision": "b" * 64, "status": "ready"}
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE router_operations (operation_id TEXT PRIMARY KEY, result_json TEXT)")
    database.execute(
        "INSERT INTO router_operations VALUES (?, ?)",
        (module._operation_key("fixture-owner", "fixture-save"), json.dumps(original)),
    )
    database.commit()
    queries = []
    database.set_trace_callback(queries.append)

    def forbidden_write(*args, **kwargs):
        pytest.fail("Unknown-operation recovery must never create or replay a write")

    def owned(task_id, owner):
        if task_id != current["task_id"] or owner != "fixture-owner":
            raise module.ConnectorError(404, "task not found")
        return deepcopy(current)

    reader = object.__new__(module.ConnectorAPI)
    reader.contract = SimpleNamespace(connect=lambda: database, operation=forbidden_write)
    reader.owned = owned
    reader.public = deepcopy
    changes_before = database.total_changes
    yield SimpleNamespace(
        reader=reader, original=original, current=current,
        operation_id="fixture-save", owner="fixture-owner", module=module,
    )
    try:
        assert database.total_changes == changes_before
        assert all(query.lstrip().upper().startswith("SELECT ") for query in queries)
    finally:
        database.close()


@pytest.fixture
def example_arguments():
    common = {"task_id": "fixture-task", "expected_revision": "a" * 64}
    original_prompt = "  原文：雨夜，缓慢推镜。\nNative rain sound.\t保留空白。  "
    return {
        "h3_capabilities": {},
        "h3_prompt_guidance": {},
        "h3_save_draft": {
            "operation_id": "fixture-save", "original_prompt": original_prompt,
            "prompt": original_prompt, "verbatim": True, "recipe_id": "A4",
            "skill_sources": [{"name": "fixture-local-skill", "source": "local_workbuddy"}],
            "mode": "t2v", "duration": 15, "width": 480, "height": 864,
            "fps": 24, "orientation": "portrait", "audio_policy": "native",
        },
        "h3_confirm_prompt": {
            **common, "operation_id": "fixture-confirm", "expected_output_id": "fixture-context",
        },
        "h3_start_preview": {
            **common, "operation_id": "fixture-start", "expected_output_id": "fixture-context",
            "expected_run_id": None,
        },
        "h3_list_tasks": {"limit": 20, "offset": 0},
        "h3_get_task": {"task_id": "fixture-task"},
        "h3_review_preview": {
            **common, "operation_id": "fixture-review", "output_id": "fixture-video",
            "expected_run_id": "fixture-preview-run", "decision": "reject",
            "feedback": "用户拒绝本输出；不重新生成。",
        },
        "h3_cancel_task": {
            **common, "operation_id": "fixture-cancel", "expected_run_id": "fixture-preview-run",
            "stage_id": "preview",
        },
    }


def test_package_contains_only_declared_static_artifacts_and_material_guidance():
    entries = list(PACKAGE.rglob("*"))
    assert not any(entry.is_symlink() for entry in entries)
    assert {
        entry.relative_to(PACKAGE).as_posix() for entry in entries if entry.is_file()
    } == PACKAGE_FILES | {"skills/h3-studio/references/multimodal.md"}


@pytest.mark.parametrize("filename", SCHEMAS)
def test_artifact_matches_documented_field_subset_and_package_contract(filename, artifacts):
    schema = SCHEMAS[filename]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(artifacts[filename])


@pytest.mark.parametrize(
    "filename,path,replacement",
    [
        ("connector-meta.json", ("auth_mode",), "gateway"),
        ("connector-meta.json", ("minWorkbuddyVersion",), "4.23.0"),
        ("connector-meta.json", ("type",), "cli"),
        ("connector-meta.json", ("examples_en",), []),
        ("mcp.json", ("mcpServers", SOURCE, "type"), "StreamableHTTP"),
        ("mcp.json", ("mcpServers", SOURCE, "url"), ENDPOINT.replace("https:", "http:")),
        ("mcp.json", ("mcpServers", SOURCE, "url"), ENDPOINT + "?token=${H3_ACCESS_TOKEN}"),
        ("mcp.json", ("mcpServers", SOURCE, "url"), "https://example.com/mcp/h3"),
        ("mcp.json", ("mcpServers", SOURCE, "command"), "python3"),
        ("mcp.json", ("mcpServers", SOURCE, "autoApprove"), ["h3_start_preview"]),
        ("mcp.json", ("mcpServers", "another-server"), {}),
        ("mcp.json", ("mcpServers", SOURCE, "headers", "Authorization"), "Bearer fixture-only"),
        ("token-schema.json", ("fields", 0, "key"), "OTHER_TOKEN"),
        ("token-schema.json", ("fields", 0, "type"), "text"),
        ("token-schema.json", ("fields", 0, "required"), False),
        ("token-schema.json", ("fields", 0, "defaultValue"), "fixture-only"),
        ("token-schema.json", ("fields", 0, "value"), "fixture-only"),
    ],
)
def test_schema_rejects_unsafe_or_incompatible_configuration(filename, path, replacement, artifacts):
    candidate = deepcopy(artifacts[filename])
    target = candidate
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMAS[filename]).validate(candidate)


def test_duplicate_json_fields_are_rejected():
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        json.loads('{"type": "password", "type": "text"}', object_pairs_hook=unique_object)


def test_one_private_token_is_injected_only_in_the_fixed_https_authorization_header(artifacts):
    server = artifacts["mcp.json"]["mcpServers"][SOURCE]
    address = urlsplit(server["url"])
    assert address.scheme == "https"
    assert address.hostname == "ai-x10drg.taild500c8.ts.net"
    assert address.port == 4001
    assert address.path == "/mcp/h3"
    assert not any((address.username, address.password, address.query, address.fragment))
    placeholders = re.findall(r"\$\{([A-Z0-9_]+)\}", json.dumps(artifacts["mcp.json"]))
    fields = artifacts["token-schema.json"]["fields"]
    assert placeholders == [field["key"] for field in fields] == [TOKEN_KEY]
    assert server["headers"] == {"Authorization": f"Bearer ${{{TOKEN_KEY}}}"}


@pytest.mark.parametrize("filename", sorted(PACKAGE_FILES) + ["documentation"])
def test_public_artifacts_have_no_common_plaintext_secret_forms(filename):
    path = DOCUMENTATION if filename == "documentation" else PACKAGE / filename
    assert not contains_secret(path.read_text(encoding="utf-8")), f"Potential secret in {filename}"


@pytest.mark.parametrize(
    "synthetic",
    [
        "sk-" + "a" * 24,
        "ghp_" + "a" * 36,
        "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "-----BEGIN " + "PRIVATE KEY-----",
        "Bearer " + "a" * 32,
        "api_key=" + "a" * 32,
    ],
)
def test_secret_guard_rejects_synthetic_credentials(synthetic):
    assert contains_secret(synthetic)


def test_svg_is_accessible_self_contained_and_has_no_active_content():
    raw = (PACKAGE / "icon.svg").read_text(encoding="utf-8")
    assert "<!DOCTYPE" not in raw.upper()
    assert "<!ENTITY" not in raw.upper()
    root = ElementTree.fromstring(raw)
    namespace = "{http://www.w3.org/2000/svg}"
    assert root.tag == namespace + "svg"
    assert root.attrib["viewBox"] == "0 0 64 64"
    assert root.attrib["role"] == "img"
    title = root.find(namespace + "title")
    assert title is not None and title.text
    assert root.attrib["aria-labelledby"] == title.attrib["id"]
    for element in root.iter():
        assert element.tag in {namespace + tag for tag in ("svg", "title", "rect", "path")}
        for attribute, value in element.attrib.items():
            assert not attribute.lower().startswith("on")
            assert "href" not in attribute.lower()
            assert not re.search(r"(?i)url\s*\(|javascript:|data:|https?://", value)


def test_skill_frontmatter_is_scoped_and_matches_its_directory(skill):
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", skill, re.DOTALL)
    assert frontmatter is not None
    metadata = yaml.safe_load(frontmatter.group(1))
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "h3-studio"
    assert isinstance(metadata["description"], str)
    assert "H3" in metadata["description"] and "不用于" in metadata["description"]
    assert len(skill.splitlines()) < 500


def test_skill_keeps_nine_remote_tools_and_only_two_restricted_upload_tools(skill):
    for document in (skill, DOCUMENTATION.read_text(encoding="utf-8")):
        expected = TOOL_NAMES | ({"h3_upload_asset", "h3_get_upload"} if document == skill else set())
        assert set(re.findall(r"\bh3_[a-z_]+\b", document)) == expected
        tool_rows = re.findall(r"^\| `(h3_[a-z_]+)` \|", document, re.MULTILINE)
        assert len(tool_rows) == len(TOOL_NAMES)
        assert set(tool_rows) == TOOL_NAMES


def test_skill_has_only_the_approved_recipe_and_native_serial_mode_table(skill):
    rows = dict(re.findall(r"^\| ([^|\n]+?) \| ([^|\n]+?) \|$", skill, re.MULTILINE))
    assert re.findall(r"`([^`]+)`", rows["配方"]) == ["A4", "A4_C0", "A4_C1", "B8"]
    assert "`A4`（默认）" in rows["配方"]
    assert rows["时长"] == "`15s`"
    assert rows["宽×高"] == "`480x864`（竖屏）"
    assert rows["帧率"] == "`24fps`"
    assert rows["输出"] == "`native`（原生输出，不放大）"
    assert rows["执行档位"] == "`serial4060`（单任务串行，由服务端调度）"


@pytest.mark.parametrize(
    "required_instruction",
    [
        "运行时自描述 MCP 工具 schema",
        "`inputSchema` / `outputSchema`",
        "禁止猜测请求体",
        "`task_id`、`operation_id`、`expected_revision`、`expected_output_id`",
        "schema 不支持必要的版本或输出绑定",
        "优先发现并使用 WorkBuddy 已安装且适用的本地创意 Skills",
        "没有适用本地 Skill，或其指导不足",
        "服务端蒸馏指导",
        "不要额外调用服务端 LLM",
        "逐字保留原文，包括语言、空白、标点",
        "原始输入 vs 草稿",
        "实际 Skill 来源",
        "配方明细",
        "用户的批准必须绑定所展示的任务、版本、输出和动作",
        "修改文本、配方、参数或输出后，旧批准失效",
        "已确认，尚未生成",
        "不得调用 `h3_start_preview`",
        "可连续调用 `h3_confirm_prompt` 再 `h3_start_preview`",
        "确认与启动是两个操作，不能复用同一个 ID",
        "没有输出时明确展示“无输出”",
        "禁止伪造 ID",
        "先核对回执，不能换 ID 重发",
        "禁止自动质量通过、自动重试、自动放大或自动付费",
        "禁止第三方生成器",
        "禁止回退到旧通用媒体流程",
        "每轮最多查询状态 3 次",
        "默认间隔 10 秒",
        "20 秒、40 秒退避",
        "Retry-After",
        "结果未知",
        "取消待核实",
        "不创建额外 Agent",
        "不自行选择 GPU、主机或模型后端",
        "不要索取、读取或回显令牌",
        "审阅的精确产物字段名是 `output_id`",
        "不透明 SHA-256 字符串，不是整数",
        "明确传 `verbatim: true`",
        "客户端来源声明不等于服务端已验证执行",
        "`serial4060` 是服务端执行约束，不是当前工具请求字段",
        "`decision` 只能是用户明确选择的 `approve` 或 `reject`",
        "没有视频不等于没有确认对象",
        "`expected_run_id` 取当前 `preview.run_id`",
        "`task_id` 与 `operation_id` 恰好传一个",
        "仅传原 `operation_id`，不同时传 `task_id`",
        "创建结果未知且尚无任务 ID 时也走同一路径",
        "不把随后变化的任务状态当作原操作结果",
        "绝不为同一未知操作创建新 `operation_id`",
        "禁止直接写入加密凭证库或绕过原生表单",
    ],
)
def test_skill_retains_required_safety_instructions(skill, required_instruction):
    assert required_instruction in skill


def test_fixture_import_is_disclosed_without_generation_or_quality_claims(skill):
    instruction = next(line for line in skill.splitlines() if "`fixture_import`" in line)
    assert "或“验收导入”" in instruction
    assert "必须明确告知“这是验收导入视频，非本次生成”" in instruction
    assert "不得将其作为本配方实际执行或画质通过证据" in instruction
    assert "该标记不授权导入操作，也不增加 MCP 工具" in instruction


def test_confirmation_only_diagram_does_not_lead_to_generation(skill):
    assert "Approval -->|仅确认| Confirm[h3_confirm_prompt]" in skill
    assert "Confirm --> Stop[已确认但未生成]" in skill
    assert "Approval -->|确认并生成| ConfirmGenerate[h3_confirm_prompt 成功]" in skill
    assert "Bound -->|是| Start[h3_start_preview 一次]" in skill
    assert "Bound -->|否| Redisplay[重新展示并请求确认]" in skill


def test_skill_parameter_table_matches_canonical_required_fields(skill, canonical_tools):
    rows = dict(re.findall(r"^\| `(h3_[a-z_]+)` \| ([^|\n]+) \|", skill, re.MULTILINE))
    assert set(rows) == TOOL_NAMES
    for name, details in rows.items():
        mentioned = set(re.findall(r"`([a-z_]+)`", details))
        schema = canonical_tools[name]["inputSchema"]
        assert set(schema.get("required", [])) <= mentioned, name
        assert mentioned <= set(schema["properties"]), name


@pytest.mark.parametrize("tool_name", sorted(TOOL_NAMES))
def test_offline_argument_examples_validate_against_canonical_schema(
    tool_name, canonical_tools, example_arguments,
):
    Draft202012Validator(canonical_tools[tool_name]["inputSchema"]).validate(
        example_arguments[tool_name],
    )


def test_canonical_recipe_scope_and_draft_edit_dependencies(canonical_tools, example_arguments):
    schema = canonical_tools["h3_save_draft"]["inputSchema"]
    properties = schema["properties"]
    assert properties["recipe_id"]["enum"] == ["A4", "A4_C0", "A4_C1", "B8"]
    assert properties["recipe_id"]["default"] == "A4"
    assert "serial4060" not in properties
    assert properties["mode"]["enum"] == ["t2v", "i2v", "l2v", "fl2v", "reference", "hybrid"]
    assert properties["fps"]["const"] == 24
    assert properties["duration"]["maximum"] == 15
    assert properties["assets"]["additionalProperties"] is False
    candidate = {**example_arguments["h3_save_draft"], "task_id": "fixture-task"}
    validator = Draft202012Validator(schema)
    with pytest.raises(ValidationError):
        validator.validate(candidate)
    candidate["expected_revision"] = "a" * 64
    validator.validate(candidate)


@pytest.mark.parametrize(
    "tool_name,field,value",
    [
        ("h3_confirm_prompt", "expected_revision", 1),
        ("h3_confirm_prompt", "expected_output_id", None),
        ("h3_start_preview", "expected_revision", "latest"),
        ("h3_start_preview", "expected_output_id", None),
        ("h3_review_preview", "expected_run_id", None),
        ("h3_review_preview", "decision", "auto"),
        ("h3_review_preview", "expected_output_id", "fixture-video"),
        ("h3_get_task", "operation_id", "fixture-unknown"),
        ("h3_cancel_task", "stage_id", "all"),
        ("h3_save_draft", "recipe_id", "other-mode"),
        ("h3_save_draft", "duration", 16),
        ("h3_save_draft", "width", 1280),
        ("h3_save_draft", "audio_policy", "silent"),
        ("h3_save_draft", "serial4060", True),
        ("h3_save_draft", "skill_sources", [{"name": "fixture", "source": "invented"}]),
        ("h3_save_draft", "skill_sources", [
            {"name": "fixture", "source": "local_workbuddy", "path": "undeclared-path"},
        ]),
    ],
)
def test_canonical_schema_rejects_unsafe_argument_drift(
    tool_name, field, value, canonical_tools, example_arguments,
):
    arguments = {**example_arguments[tool_name], field: value}
    with pytest.raises(ValidationError):
        Draft202012Validator(canonical_tools[tool_name]["inputSchema"]).validate(arguments)


def test_unknown_creation_recovery_uses_only_the_original_operation_id(
    canonical_tools, example_arguments,
):
    original_operation_id = example_arguments["h3_save_draft"]["operation_id"]
    lookup = {"operation_id": original_operation_id}
    definition = canonical_tools["h3_get_task"]
    validator = Draft202012Validator(definition["inputSchema"])
    validator.validate(lookup)
    assert definition["annotations"]["readOnlyHint"] is True
    assert definition["annotations"]["destructiveHint"] is False
    assert lookup == {"operation_id": "fixture-save"}
    with pytest.raises(ValidationError):
        validator.validate({})
    with pytest.raises(ValidationError):
        validator.validate({**lookup, "task_id": "fixture-task"})


def test_unknown_operation_recovery_instructions_require_an_exact_receipt_and_no_new_id(skill):
    diagrams = re.findall(r"```mermaid\n(.*?)\n```", skill, re.DOTALL)
    recovery = next(diagram for diagram in diagrams if "Unknown[" in diagram)
    assert set(re.findall(r"\bh3_[a-z_]+\b", recovery)) == {"h3_get_task"}
    assert "Unknown[写操作结果未知，保留原 operation_id] --> GetByOperation" in recovery
    assert "GetByOperation --> ExactReceipt{精确操作回执与原请求匹配}" in recovery
    assert "ExactReceipt -->|是| Recover[恢复原始结果，再只读核对当前任务]" in recovery
    assert "ExactReceipt -->|否或缺失| Halt[保持未知并停止，不创建新 operation_id 或替代任务]" in recovery
    assert "回执不存在、处理中、身份/操作/请求不匹配或缺少精确证据" in skill


def test_unknown_create_recovers_exact_original_receipt_without_new_operation(receipt_reader):
    state = receipt_reader
    arguments = {"operation_id": state.operation_id}
    response = state.reader.call("h3_get_task", arguments, state.owner)
    assert arguments == {"operation_id": "fixture-save"}
    assert response["receipt"] == {
        "operation_id": state.operation_id,
        "task_id": state.original["task_id"],
        "result_revision": state.original["revision"],
        "result": state.original,
    }
    assert response["revision"] == state.current["revision"] != state.original["revision"]
    assert response["status"] == "ready"
    assert response["receipt"]["result"]["status"] == "awaiting_prompt_approval"
    assert state.reader.call("h3_get_task", {"task_id": response["task_id"]}, state.owner) == state.current


@pytest.mark.parametrize(
    "arguments,owner",
    [
        ({"operation_id": "fixture-missing"}, "fixture-owner"),
        ({"operation_id": "fixture-save"}, "fixture-other-owner"),
        ({"task_id": "fixture-task"}, "fixture-other-owner"),
    ],
)
def test_missing_or_unowned_receipt_never_creates_a_replacement(receipt_reader, arguments, owner):
    with pytest.raises(receipt_reader.module.ConnectorError) as failure:
        receipt_reader.reader.call("h3_get_task", arguments, owner)
    assert failure.value.status_code == 404


@pytest.mark.parametrize(
    "arguments", [{}, {"task_id": "fixture-task", "operation_id": "fixture-save"}],
)
def test_actual_lookup_rejects_missing_or_ambiguous_selectors(receipt_reader, arguments):
    with pytest.raises(receipt_reader.module.ConnectorError) as failure:
        receipt_reader.reader.call("h3_get_task", arguments, receipt_reader.owner)
    assert failure.value.status_code == 400


def test_docs_distinguish_offline_artifacts_from_unverified_runtime_acceptance():
    document = DOCUMENTATION.read_text(encoding="utf-8")
    for required in (
        "https://open.workbuddy.cn/docs/connector",
        "没有部署、导入 WorkBuddy、调用生产 API 或执行 GPU 推理",
        "不代表线上已可用",
        "未修改共享 `siyuan-media` Skill",
        "SDK、认证授权、工具执行和状态持久化",
        "AgentCatalog", "TaskEnvelope", "CapabilityManifest", "ResultEnvelope",
        "Skill 指令不是安全强制执行机制",
        "不是 WorkBuddy 官方验证器",
        "不能证明助手实际遵循指令或检测所有秘密",
        "GPU 推理与真实视频质量验收必须另行授权",
        "tests/test_h3_workbuddy_connector.py",
        "1.26.0",
        "sse-starlette 3.4.11",
        "探索性证据，不作为最终版本验收",
        "deploy/h3-mcp/studio/connector_api.py",
        "TOOL_DEFINITIONS",
        "不跳过参数对齐",
        "不能把其他连接器的 `type: http` 条目",
        "待用户明确批准维护窗口",
        "不能证明远程自动安装",
        "ivan-laptop",
        "(Get-Item WorkBuddy.exe).VersionInfo",
        "ProductVersion=5.5.4.0",
        "FileVersion=5.5.4",
        "但不是 UI 兼容性验收",
        "版本检查没有安装软件、修改 MCP 配置或读取凭证",
    ):
        assert required in document
