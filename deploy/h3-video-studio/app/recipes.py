from __future__ import annotations


RECIPE_IDS = ("A4", "A4_C0", "A4_C1", "B8")
EXECUTION_FIELDS = ("recipe_id", "recipe_version", "backend_id", "runtime_version",
                    "gpu_uuid", "execution_seconds")


def recipe_scope(project, stage="preview"):
    return (stage == "preview" and project.get("mode") == "t2v"
            and project.get("duration") == 15 and project.get("orientation") == "portrait"
            and project.get("audio_policy") == "native")


def require_recipe_id(value):
    if not isinstance(value, str) or value not in RECIPE_IDS:
        raise ValueError("请显式选择新配方 A4 / A4_C0 / A4_C1 / B8；历史配方不能直接再运行。")
    return value


def confirmed_recipe(catalog, recipe_id):
    require_recipe_id(recipe_id)
    if not isinstance(catalog, dict) or catalog.get("enabled") is not True:
        raise RuntimeError("配方调度未就绪：Fleet 尚未明确启用目录。")
    entries = catalog.get("recipes")
    if not isinstance(entries, list):
        raise RuntimeError("配方调度未就绪：缺少服务端目录。")
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("recipe_id") == recipe_id]
    if len(matches) != 1 or not isinstance(matches[0].get("version"), str) or not matches[0]["version"]:
        raise RuntimeError("配方调度未就绪：服务端未确认该配方版本。")
    return matches[0]


def execution_info(job):
    execution = job.get("execution") or {}
    contract = execution.get("contract") or {}
    return {key: source[key] for source in (contract, execution, job) for key in EXECUTION_FIELDS
            if source.get(key) is not None}
