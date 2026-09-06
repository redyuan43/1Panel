"""Isolated console fixture; no production credentials, state, or model calls."""
import argparse
import asyncio
import os
from pathlib import Path
import sys
import time

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_router.config import Registry, Settings
from ai_router.control import create_app
from ai_router.evaluator import Evaluation
from ai_router.route_trace import DecisionTrace
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from test_core import FakeHealth, SimpleTokenCounter, healthy


async def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for key in list(os.environ):
        if key.startswith("AI_ROUTER_"):
            del os.environ[key]
    os.environ.update({
        "AI_ROUTER_ADMIN_KEY": "ui-preview-only",
        "AI_ROUTER_1PANEL_API_KEY": "ui-preview-client",
        "AI_ROUTER_LITELLM_MASTER_KEY": "ui-preview-internal",
        "AI_ROUTER_STATE_KEY": Fernet.generate_key().decode(),
        "AI_ROUTER_AUDIT_PATH": str(directory / "audit.jsonl"),
        "AI_ROUTER_ROUTE_TRACE_DB_PATH": str(directory / "traces.sqlite3"),
        "AI_ROUTER_TRAINING_ENABLED": "false",
    })
    settings = Settings(defaults_path=ROOT / "config/defaults.yaml",
                        runtime_path=directory / "settings.yaml")
    registry = Registry(ROOT / "config/registry.yaml")
    runtime = build_runtime(settings=settings, registry=registry, store=InMemoryStateStore(),
                            token_counter=SimpleTokenCounter(), instance_id="ui-preview")
    await runtime.health.client.aclose()
    runtime.health = FakeHealth({endpoint.id: healthy(endpoint.id, context=endpoint.safe_context_tokens)
                                for endpoint in registry.endpoints})
    runtime.policy.health = runtime.health

    def reject_network(request):
        raise RuntimeError("Model/network calls are disabled in UI preview")

    for component in (runtime.evaluator, runtime.compactor):
        await component.client.aclose()
        component.client = httpx.AsyncClient(transport=httpx.MockTransport(reject_network))
    await runtime.internal_client.aclose()
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(reject_network))
    for index, kind in enumerate(("local", "cloud", "identity", "rejected")):
        trace = DecisionTrace(
            request_id=f"preview-{kind}", client_id="1panel", key_id="preview",
            protocol="chat", requested_model="auto", excerpt={"text": f"Synthetic {kind} audit", "tool_names": []},
            instance_id="ui-preview", boot_id="preview", settings_hash="preview", registry_hash="preview",
        )
        trace.payload["started_at"] = time.time() - 60 + index * 10
        trace.set_request_context(conversation_id="preview-conversation",
                                  lineage_relation="new" if index == 0 else "continuation",
                                  branch_id=f"preview-branch-{index}",
                                  parent_branch_id=f"preview-branch-{index - 1}" if index else None)
        if kind == "identity":
            trace.finish_identity_intercept(status_code=200, input_tokens=10, output_tokens=20)
        elif kind == "rejected":
            trace.fail(status_code=403, code="invalid_api_key", message="Synthetic authorization rejection")
        else:
            endpoint = next(item for item in registry.endpoints
                            if item.id == "ai-qwen38-27b") if kind == "local" else next(
                                item for item in registry.endpoints if item.cloud)
            trace.set_evaluation(Evaluation("general", None, 1, "preview"))
            trace.record(1, "candidate_scope", "passed", branch="auto", reason="compatible_candidates", evidence={})
            trace.record(1, "conversation_affinity", "evaluated", branch="intelligent_v2",
                         reason="no_bound_conversation", evidence={})
            trace.record(1, "local_sufficiency", "passed" if kind == "local" else "rejected",
                         branch="local_sufficient" if kind == "local" else "remote_required",
                         reason="local_candidates_available" if kind == "local" else "local_capacity_full", evidence={})
            if kind == "cloud":
                trace.record(1, "remote_expert_dispatch", "selected", reason="configured_remote_order", evidence={})
            trace.record(1, "score_candidates", "passed", reason="scored", evidence={})
            trace.record(1, "deployment_binding", "passed", reason="bound", evidence={})
            trace.record(1, "capacity_check", "passed", reason="capacity_acquired", evidence={})
            trace.set_selection(attempt=1, selected_model=endpoint.public_model, endpoint_id=endpoint.id,
                                deployment_id="preview-deployment", task="general",
                                reason="local_sufficient" if kind == "local" else "configured_remote_order",
                                affinity="new" if index == 0 else "hit")
            trace.confirm_selection(attempt=1)
            trace.finish(attempt=1, status_code=200)
        await runtime.route_traces.save(trace)
        if kind != "rejected":
            await runtime.route_traces.save_privacy_assessment({
                "request_id": trace.request_id, "updated_at": time.time(), "status": "completed",
                "decision": "internal_info" if kind == "identity" else "normal",
                "reason": "internal_identity" if kind == "identity" else "technical_task",
                "review_model": "preview-reviewer", "policy_version": "preview", "valid": True,
            })
    for index in range(105):
        trace = DecisionTrace(
            request_id=f"preview-long-{index}", client_id="1panel", key_id="preview",
            protocol="chat", requested_model="auto", excerpt={"text": f"Synthetic retained round {index}", "tool_names": []},
            instance_id="ui-preview", boot_id="preview", settings_hash="preview", registry_hash="preview",
        )
        trace.payload["started_at"] = time.time() - 10000 + index
        trace.set_request_context(conversation_id="preview-long-conversation", lineage_relation="continuation")
        trace.finish_identity_intercept(status_code=200, input_tokens=10, output_tokens=20)
        await runtime.route_traces.save(trace)
    return runtime


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=14801)
    args = parser.parse_args()
    runtime = await prepare(args.state_dir.resolve())
    app = create_app(runtime)

    @app.middleware("http")
    async def isolate_management_actions(request, call_next):
        path = request.url.path
        allowed_write = (
            request.method == "PUT" and path == "/api/settings"
        ) or (
            request.method == "POST"
            and path == "/api/prompt-directives/suggest"
        ) or (
            request.method == "POST" and path.startswith("/api/route-traces/")
            and path.endswith(("/reviews", "/privacy-feedback"))
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not allowed_write:
            return JSONResponse({"error": {"code": "preview_read_only", "message": "Preview management action disabled"}},
                                status_code=403)
        return await call_next(request)

    @app.get("/__ui_preview__")
    async def preview_marker():
        return {"isolated": True, "state_dir": str(args.state_dir.resolve())}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                          log_level="warning", access_log=False))
    try:
        await server.serve()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
