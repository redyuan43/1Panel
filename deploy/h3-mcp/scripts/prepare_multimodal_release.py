from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_preparer", BASE / "scripts/prepare_release.py")
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


def replace(source, before, after):
    if source.count(before) != 1:
        raise ValueError("release base anchor changed: " + before[:80])
    return source.replace(before, after)


def studio_overlay(files):
    files = dict(files)
    for name in ("connector_api.py", "connector_assets.py", "input_contract.py", "input_view.py", "browser_tasks.py", "multimodal_client.py", "mcp_settings.py"):
        files["app/" + name] = (BASE / "studio" / name).read_bytes()
    files["app/reference_video.py"] = (BASE / "shared/reference_video.py").read_bytes()
    files["app/connector_schema.json"] = preparer.connector_schema(files["app/connector_api.py"])
    source = files["app/router_contract.py"].decode()
    source = replace(source, "    install_connector_api(module, contract)\n", "    install_connector_api(module, contract)\n    from .input_view import install as install_input_view\n    install_input_view(module, contract)\n")
    files["app/router_contract.py"] = source.encode()
    source = files["app/main.py"].decode()
    source = replace(source, '            workflow, template = {}, "fleet-recipe:" + recipe["recipe_id"]\n        else:',
        '            workflow, template = {}, "fleet-recipe:" + recipe["recipe_id"]\n'
        '        elif stage_id == "preview" and project.get("execution_profile"):\n'
        '            recipe = {"profile_id": project["execution_profile"]["profile_id"],\n'
        '                      "profile_version": project["execution_profile"]["version"],\n'
        '                      "input_sha256": project["connector_input_sha256"],\n'
        '                      "assets": {kind: {key: asset[key] for key in ("asset_id", "sha256", "comfy_name", "size")}\n'
        '                                 for kind, asset in project["assets"].items()}}\n'
        '            workflow, template = build_workflow(project, stage_id, SETTINGS.workflow_root)\n'
        '        else:')
    files["app/main.py"] = source.encode()
    source = files["app/fleet.py"].decode()
    source = replace(source, '        if recipe is not None:\n            if stage != "preview"',
        '        if recipe is not None and "profile_id" in recipe:\n'
        '            return self.submit_multimodal(workflow, execution_id, stage, profile, recipe=recipe, prepared=prepared)\n'
        '        if recipe is not None:\n            if stage != "preview"')
    files["app/fleet.py"] = source.encode()
    source = files["app/recipes.py"].decode()
    source = replace(source, '"gpu_uuid", "execution_seconds", "admission_reason")',
        '"gpu_uuid", "execution_seconds", "admission_reason", "phase", "model_state", "phase_timings", "timing_basis")')
    files["app/recipes.py"] = source.encode()
    source = files["frontend/app.js"].decode()
    source = replace(source, 'function showSetup() {', 'function showSetup() {\n  window.dispatchEvent(new Event("h3-input-edit-reset"));')
    source = replace(source, 'function projectEstimateLabel(summary) {', 'function projectEstimateLabel(summary) {\n  if (summary?.label) return summary.label;')
    source = replace(source, '  cloud.disabled = state.mode === "hybrid" || state.audioPolicy === "lock_source";', '  cloud.disabled = true;')
    source = replace(source, '    state.project = await api("/api/projects", {\n      method: "POST",\n      body: formData,\n    });',
        '    state.project = await window.h3SaveBrowserDraft(formData);')
    source = replace(source, '    await api(`/api/projects/${state.project.id}/context-ir`, { method: "POST" });\n    await refreshCurrentProject();',
        '    await refreshCurrentProject();')
    source = replace(source, 'async function startContextIR() {', 'async function startContextIR() {\n  if (state.project?.connector_owner) { $("commonStageError").textContent = "请使用修改输入保存新版本，不重复改写已确认提示词。"; $("commonStageError").classList.remove("hidden"); return; }')
    source = replace(source, 'async function approveContextIR() {', 'async function approveContextIR() {\n  if (state.project?.connector_owner) return runAction(() => window.h3BrowserAction("h3_confirm_prompt", {expected_output_id: state.project.stages.context_ir.output_id}));')
    source = replace(source, 'async function startStage(stageId, newSeed) {',
        'async function startStage(stageId, newSeed) {\n'
        '  if (state.project?.connector_owner) {\n'
        '    if (stageId !== "preview" || newSeed || (state.recipeSelections[state.project.id] && state.recipeSelections[state.project.id] !== state.project.recipe_id)) { $("commonStageError").textContent = "配置或种子变更请使用修改输入，保存新版本并重新确认。"; $("commonStageError").classList.remove("hidden"); return; }\n'
        '    return runAction(() => window.h3BrowserAction("h3_start_preview", {expected_output_id: state.project.stages.context_ir.output_id, expected_run_id: state.project.stages.preview.run_id || null}));\n'
        '  }')
    source = replace(source, 'async function approveStage(stageId) {',
        'async function approveStage(stageId) {\n'
        '  if (state.project?.connector_owner) return runAction(() => window.h3BrowserAction("h3_review_preview", {output_id: state.project.stages.preview.output_id, expected_run_id: state.project.stages.preview.run_id, decision: "approve"}));')
    source = replace(source, 'async function cancelStage(stageId) {',
        'async function cancelStage(stageId) {\n'
        '  if (state.project?.connector_owner) return runAction(() => window.h3BrowserAction("h3_cancel_task", {stage_id: stageId, expected_run_id: state.project.stages[stageId].run_id || null}));')
    source = replace(source, "  renderStageActions(stage);\n}", "  renderStageActions(stage);\n  window.dispatchEvent(new CustomEvent('h3-project-rendered', {detail: project}));\n}")
    source = replace(source, '      : stage.execution?.recipe_id === "B8"', '      : project.execution_profile\n      ? `完整模型 ${project.execution_profile.steps} 步`\n      : (stage.execution?.recipe_id || project.recipe_id) === "B8"')
    source = replace(source, '? "Turbo 4"', '? (project.recipe_id ? "LightX2V 4 步" : "历史 Turbo 配置")')
    files["frontend/app.js"] = source.encode()
    source = files["frontend/index.html"].decode()
    source = replace(source, "</head>", '<link rel="stylesheet" href="./input-assets.css">\n<script defer src="./input-assets.js"></script>\n<script defer src="./input-edit.js"></script>\n</head>')
    source = replace(source, '<option value="cloud">官方 Context IR 优化（API 调用）</option>', '<option value="cloud" disabled>本轮仅低清；请先通过创作 Skills 整理提示词</option>')
    source = replace(source, '<option value="manual">直接使用原文（不调用 API）</option>', '<option value="manual" selected>直接使用原文（不调用 API）</option>')
    source = replace(source, '<option value="portrait">竖版 · 预览 480×864</option>', '<option value="portrait" selected>竖版 · 预览 480×864</option>')
    source = replace(source, '<button type="button" data-value="cloud" class="strategy-option">', '<button type="button" data-value="cloud" class="strategy-option" disabled>')
    files["frontend/index.html"] = source.encode()
    for filename in ("input-assets.js", "input-assets.css", "input-edit.js"):
        files["frontend/" + filename] = (BASE / "frontend" / filename).read_bytes()
    for filename in ("mcp-settings.html", "mcp-settings.js", "mcp-settings.css"):
        files["frontend/" + filename] = (BASE / "frontend" / filename).read_bytes()
    for name, content in files.items():
        if name.endswith(".py"):
            preparer.parse_python(content, name)
    return files


def fleet_overlay(files):
    files = dict(files)
    for filename in ("backend_lifecycle.py", "multimodal_catalog.py", "multimodal_api.py", "execution_phases.py", "multimodal_qualification.py", "input_memory.py"):
        files["app/" + filename] = (BASE / "fleet" / filename).read_bytes()
    files["app/reference_video.py"] = (BASE / "shared/reference_video.py").read_bytes()
    source = files["app/main.py"].decode()
    source = 'from .multimodal_catalog import MultimodalCatalog\n' + source if not source.startswith('from __future__') else source.replace(
        'from __future__ import annotations\n', 'from __future__ import annotations\n\nfrom .multimodal_catalog import MultimodalCatalog\n', 1)
    source = replace(source, "RecipeDispatcher(self, RecipeCatalog(), Path(os.environ.get(",
        'RecipeDispatcher(self, MultimodalCatalog(RecipeCatalog(), os.environ.get("H3_MULTIMODAL_CATALOG")), Path(os.environ.get(')
    source = replace(source, '            reason = "fleet_draining" if self.draining else self.store.release_gate_reason(job.get("execution_id"))',
        '            lifecycle = getattr(self, "backend_lifecycle", None)\n'
        '            reason = "backend_disabled" if lifecycle and not lifecycle.policy()["enabled"] else "fleet_draining" if self.draining else self.store.release_gate_reason(job.get("execution_id"))')
    source = replace(source, '            if job.get("recipe_id"):\n                await self.recipes.tick()\n',
        '            if job.get("recipe_id"):\n                if not getattr(self, "backend_lifecycle", None):\n                    await self.recipes.tick()\n')
    source += '\nfrom .multimodal_qualification import install as install_multimodal_qualification\ninstall_multimodal_qualification(fleet.recipes)\nfrom .backend_lifecycle import install as install_backend_lifecycle\ninstall_backend_lifecycle(fleet, app, router_protected)\nfrom .multimodal_api import install as install_multimodal_api\ninstall_multimodal_api(fleet, app, router_protected)\n'
    source += '\nimport sys\nfrom .execution_phases import install as install_execution_phases\ninstall_execution_phases(sys.modules[__name__])\n'
    files["app/main.py"] = source.encode()
    source = files["app/recipe_dispatch.py"].decode()
    source = replace(source, '        source = catalog.get(recipe_id)["runtime_source"]',
        '        entry = catalog.get(recipe_id)\n'
        '        if entry.get("runtime_version_required") and entry["runtime_version_required"] != backend["runtime_version"]:\n'
        '            raise ValueError("multimodal_runtime_version_mismatch")\n'
        '        if entry.get("profile_id") and not backend.get("input_root"):\n'
        '            raise ValueError("multimodal_registered_input_directory_required")\n'
        '        source = entry["runtime_source"]')
    source = replace(source, '        budget = candidate_budget(recipe_id, self.profile_key(recipe_id, backend),',
        '        if recipe_id in getattr(self.catalog, "entries", {}):\n'
        '            floors.append(max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24)) * GIB)\n'
        '        budget = candidate_budget(recipe_id, self.profile_key(recipe_id, backend),')
    source = replace(source, '"memory_budget_bytes": 18 * GIB, "disk_budget_bytes": GIB}',
        '"memory_budget_bytes": demand_budget(metadata["contract"], (max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24))\n'
        '                                                                                    if binding.get("profile_id") else 18) * GIB), "disk_budget_bytes": GIB}')
    source = replace(source, '    def candidate(self, recipe_id: str, backend: dict) -> dict:',
        '    def candidate(self, recipe_id: str, backend: dict, job=None) -> dict:')
    source = replace(source, '        floors = []\n        for row in rows:',
        '        floors = []\n        model_floors = []\n        for row in rows:')
    source = replace(source, '            floors.extend((previous["candidate_budget_bytes"], peak_delta + max(2 * GIB, (peak_delta + 9) // 10)))',
        '            floors.extend((previous["candidate_budget_bytes"], peak_delta + max(2 * GIB, (peak_delta + 9) // 10)))\n'
        '            model_floors.append(previous.get("model_base_budget_bytes", max(previous["candidate_budget_bytes"], floors[-1])))')
    source = replace(source, '        return {**budget, "recipe_id": recipe_id, "lane_id": backend["lane_id"], "backend_id": backend["id"],',
        '        from .input_memory import apply_input_budget, job_contract\n'
        '        base = candidate_budget(recipe_id, self.profile_key(recipe_id, backend),\n'
        '                                max(self.policy.get("static_budget_gib", 18),\n'
        '                                    max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24))\n'
        '                                    if recipe_id in getattr(self.catalog, "entries", {}) else 18) * GIB,\n'
        '                                entry.get("peak_history"), model_floors)\n'
        '        required = "reference_video" in self.catalog.get(recipe_id).get("asset_roles", {})\n'
        '        budget = apply_input_budget(budget, job_contract(job), base["candidate_budget_bytes"], required=required)\n'
        '        return {**budget, "recipe_id": recipe_id, "lane_id": backend["lane_id"], "backend_id": backend["id"],')
    source = replace(source, '    def decision(self, recipe_id: str, backend: dict, rows: list[dict], sample: dict, health: dict) -> dict:',
        '    def decision(self, recipe_id: str, backend: dict, rows: list[dict], sample: dict, health: dict, job=None) -> dict:')
    source = replace(source, '            candidate = self.candidate(recipe_id, backend)',
        '            candidate = self.candidate(recipe_id, backend, job=job)')
    source = replace(source, 'decision = self.decision(row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]])',
        'decision = self.decision(row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]], job=row)')
    source = replace(source, 'decision = self.decision(job["recipe_id"], backend, active, self.snapshot, health_now)',
        'decision = self.decision(job["recipe_id"], backend, active, self.snapshot, health_now, job=current)')
    source = replace(source, 'row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]])["reasons"]',
        'row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]], row)["reasons"]')
    source = replace(source, '        await self.tick()\n        return self.submission_public(self.fleet.store.get(job["prompt_id"]))',
        '        if not getattr(self.fleet, "backend_lifecycle", None):\n            await self.tick()\n        return self.submission_public(self.fleet.store.get(job["prompt_id"]))')
    source = replace(source, '        return {**self.catalog.public(), "enabled": self.enabled,',
        '        public = self.catalog.public()\n'
        '        for profile in public.get("multimodal_profiles", []):\n'
        '            eligible = [backend for backend in self.backends.values() if self.qualifications(profile["profile_id"], backend)]\n'
        '            profile["qualified"] = bool(eligible)\n'
        '            profile["qualification"] = "runtime_evidence_accepted" if eligible else "runtime_not_qualified"\n'
        '        return {**public, "enabled": self.enabled,')
    source = replace(source, '        request_digest = digest(payload)\n',
        '        if binding.get("profile_id"):\n'
        '            from .multimodal_api import verify_assets, asset_root, verified_memory\n'
        '            metadata["contract"].update({key: binding[key] for key in ("mode", "width", "height", "frame_count", "actual_duration", "fps", "steps")})\n'
        '            try:\n'
        '                await asyncio.to_thread(verify_assets, metadata["contract"]["assets"], binding, asset_root(self.fleet))\n'
        '                metadata["contract"]["verified_input_memory"] = await asyncio.to_thread(\n'
        '                    verified_memory, metadata["contract"]["assets"], asset_root(self.fleet))\n'
        '                if not re.fullmatch(r"[a-f0-9]{64}", metadata["contract"]["input_sha256"]):\n'
        '                    raise ValueError("input digest required")\n'
        '            except (ValueError, KeyError, TypeError, OSError) as error:\n'
        '                raise HTTPException(400, "multimodal input verification failed") from error\n'
        '        else:\n'
        '            metadata["contract"].pop("verified_input_memory", None)\n'
        '        from .input_memory import demand_budget\n'
        '        request_digest = digest(payload)\n')
    source = replace(source, '            job = self.fleet.store.create(prompt_id=uuid.uuid4().hex, upstream_prompt_id="", execution_id=execution_id,',
        '            from .multimodal_qualification import check_submission\n'
        '            check_submission(self, recipe_id, binding, execution_id)\n'
        '            job = self.fleet.store.create(prompt_id=uuid.uuid4().hex, upstream_prompt_id="", execution_id=execution_id,')
    source = replace(source, '            backend_identity(backend)\n            if time.time() - decision["observed_at"] > 10:',
        '            backend_identity(backend)\n'
        '            payload = json.loads(reserved["request_json"])\n'
        '            contract = payload["extra_data"]["h3"].get("contract", {})\n'
        '            if contract.get("profile_id"):\n'
        '                from .multimodal_api import materialize\n'
        '                await asyncio.to_thread(materialize, self.fleet, backend, contract)\n'
        '            if time.time() - decision["observed_at"] > 10:')
    source = replace(source, '        for recipe_id in ("A4", "A4_C0", "A4_C1", "B8"):',
        '        for recipe_id in ("A4", "A4_C0", "A4_C1", "B8", *getattr(self.catalog, "entries", {})):')
    files["app/recipe_dispatch.py"] = source.encode()
    for name, content in files.items():
        if name.endswith(".py"):
            preparer.parse_python(content, name)
    return files


def control_overlay(proxy_source, image_id):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Control base must be the inspected immutable image ID")
    source = proxy_source.decode()
    source = replace(source, 'STATIC |= {"mcp-settings.html", "mcp-settings.js", "mcp-settings.css", "h3-workbuddy-auth.zip", "h3-workbuddy-auth.json"}',
        'STATIC |= {"mcp-settings.html", "mcp-settings.js", "mcp-settings.css", "h3-workbuddy-auth.zip", "h3-workbuddy-auth.json"}\n'
        'STATIC |= {"input-assets.js", "input-assets.css", "input-edit.js"}')
    source = replace(source, '"api/scripts/", "api/mcp-admin/"', '"api/scripts/", "api/mcp-admin/", "api/h3-browser/"')
    source = replace(source, 'for name in ("content-type", "range", "if-range"):', 'for name in ("content-type", "range", "if-range", "x-h3-upload-metadata"):')
    files = {"ai_router/studio_proxy.py": source.encode(),
             "ai_router/h3_mcp.py": (BASE.parent / "ai-router/ai_router/h3_mcp.py").read_bytes(),
             "ai_router/h3_mcp_assets.py": (BASE.parent / "ai-router/ai_router/h3_mcp_assets.py").read_bytes(),
             "ai_router/h3_mcp_schema.json": preparer.connector_schema((BASE / "studio/connector_api.py").read_bytes())}
    for name, content in files.items():
        if name.endswith(".py"):
            preparer.parse_python(content, name)
    copies = "".join("COPY " + name + " /app/" + name + "\n" for name in sorted(files))
    files["Dockerfile"] = ("FROM " + image_id + "\n" + copies).encode()
    files[".dockerignore"] = ("*\n!Dockerfile\n!ai_router/\nai_router/*\n" + "".join("!" + name + "\n" for name in sorted(files) if name.startswith("ai_router/"))).encode()
    return files


def main(destination):
    if destination.exists():
        raise ValueError("candidate already exists; never overwrite")
    live = Path(subprocess.check_output(["systemctl", "--user", "show", "h3-studio-ivan.service", "-p", "WorkingDirectory", "--value"], text=True).strip())
    if not live.is_relative_to(Path.home() / ".local/state/h3-studio-ivan-production/releases"):
        raise ValueError("unexpected Studio release base")
    files = {path.relative_to(live).as_posix(): path.read_bytes() for path in live.rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.name != "candidate-manifest.json"}
    result = preparer.write_candidate(destination, studio_overlay(files), {"kind": "studio-multimodal",
        "base_provenance": {"source": str(live), "files_sha256": {name: preparer.digest(content) for name, content in files.items()}}})
    print(json.dumps({"candidate": str(destination), "deployed": False, "files": len(result["files_sha256"])}))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
