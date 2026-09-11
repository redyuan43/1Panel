from __future__ import annotations

import json
import urllib.error
import urllib.parse
from pathlib import Path


def install(module):
    from .input_contract import digest
    client = module.COMFY
    original_upload = client.upload_assets

    def upload_assets(assets):
        if not assets:
            return original_upload(assets)
        legacy = {kind: asset for kind, asset in assets.items() if not asset.get("asset_id")}
        if legacy:
            original_upload(legacy)
        for asset in assets.values():
            if not asset.get("asset_id"):
                continue
            endpoint = "/api/router/input-assets/" + urllib.parse.quote(asset["comfy_name"], safe="")
            try:
                receipt = client._request("GET", endpoint)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise RuntimeError("Fleet 素材回执暂不可读；未重复上传。") from error
                try:
                    with Path(asset["path"]).open("rb") as source:
                        receipt = client._request("PUT", endpoint + "?" + urllib.parse.urlencode({"sha256": asset["sha256"], "size": asset["size"]}),
                            data=source, content_type="application/octet-stream", timeout=150)
                except OSError as upload_error:
                    raise RuntimeError("Fleet 素材上传结果未知；再次提交前必须先查询回执。") from upload_error
            if receipt.get("state") != "ready" or any(receipt.get(key) != asset[key] for key in ("sha256", "size")):
                raise RuntimeError("Fleet 原始素材摘要不一致，未提交生成。")

    def submit(workflow, execution_id, stage, profile, *, recipe, prepared=None):
        if stage != "preview":
            raise RuntimeError("多素材连接器仅开放低清预览。")
        request = {"profile_id": recipe["profile_id"], "profile_version": recipe["profile_version"],
                   "input_sha256": recipe["input_sha256"], "assets": recipe["assets"], "prompt": workflow}
        try:
            built = client._request("POST", "/api/router/multimodal-workflow", request)
            binding = built["binding"]
            if (built.get("enabled") is not True or binding.get("profile_id") != recipe["profile_id"]
                    or binding.get("profile_version") != recipe["profile_version"]
                    or binding.get("graph_sha256") != digest(workflow)
                    or binding.get("input_sha256") != recipe["input_sha256"]):
                raise ValueError("multimodal execution binding mismatch")
            if prepared:
                prepared(workflow, binding)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError("多素材工作流未通过 Fleet 配置/素材校验，未提交生成。") from error
        payload = {"prompt": workflow, "extra_data": {"h3": {"execution_id": execution_id, "stage": stage,
            "profile": "preview", "studio": True, "recipe_id": binding["profile_id"],
            "recipe_version": binding["profile_version"], "contract": binding}}}
        try:
            response = client._request("POST", "/prompt", payload)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code in {400, 401, 403, 409, 413}:
                raise RuntimeError("Fleet 拒绝多素材任务：HTTP " + str(error.code)) from error
            raise module.SubmissionUnknown("提交结果未知；保留同一执行编号，先查询对账。") from error
        if not response.get("prompt_id"):
            raise module.SubmissionUnknown("未收到有效执行回执；保留同一执行编号，不重复提交。")
        return response["prompt_id"]

    client.submit_multimodal = submit
    client.upload_assets = upload_assets
