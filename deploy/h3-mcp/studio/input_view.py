from __future__ import annotations

import os


def install(module, contract):
    from .multimodal_client import install as install_client
    install_client(module)
    from .browser_tasks import install as install_browser
    install_browser(module, module.app.state.h3_connector)
    original = module._public_project
    original_builder = module.build_workflow

    def build(project, stage, workflow_root):
        if stage == "preview" and project.get("execution_profile"):
            from .input_contract import bind_graph_assets, check_quality_controls, profile
            if project["execution_profile"] != profile(module, project):
                raise ValueError("multimodal execution profile changed")
            graph, template = original_builder(project, "proof", workflow_root)
            bind_graph_assets(graph, project)
            check_quality_controls(graph, project)
            return graph, template
        return original_builder(project, stage, workflow_root)

    def public(project):
        from .input_contract import public_assets
        value = original(project)
        value["input_assets"] = public_assets(project)
        value["input_sha256"] = project.get("connector_input_sha256")
        if project.get("connector_owner"):
            value["connector_revision"] = module.app.state.h3_connector.public(project)["revision"]
            value["connector_generation_enabled"] = os.environ.get("H3_CONNECTOR_GENERATION_ENABLED") == "true"
            value["pipeline"] = [stage for stage in value["pipeline"] if stage["id"] in {"context_ir", "preview"}]
            value["runtime_summary"] = {"label": "本轮仅低清预览，耗时以实际执行为准", "low_seconds": None, "high_seconds": None, "dynamic_cloud_stages": 0}
            if project.get("execution_profile"):
                for stage in value["pipeline"]:
                    if stage["id"] == "preview":
                        stage["runtime"] = {**stage["runtime"], "low_seconds": None, "high_seconds": None,
                            "runner": "Ivan Fleet · 单路", "label": "专用完整模型，耗时尚未实测", "basis": "多模态完整工作流；不使用旧 Turbo 耗时估计"}
        return value

    module._public_project = public
    module.build_workflow = build
