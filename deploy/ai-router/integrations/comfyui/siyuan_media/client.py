"""Small HTTP client, independent of ComfyUI/torch for isolated verification."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx


class MediaClient:
    def __init__(self, root, *, base=None, key=None, transport=None):
        base = (base or os.environ.get("SIYUAN_API_BASE", "")).rstrip("/")
        key = key or os.environ.get("SIYUAN_API_KEY", "")
        parsed = urlparse(base)
        if (not key or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") != "/v1"):
            raise ValueError("请在 ComfyUI 服务环境中配置 SIYUAN_API_BASE（含 /v1）和 SIYUAN_API_KEY。")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        private = parsed.hostname == "localhost" or parsed.hostname.endswith(".taild500c8.ts.net") or bool(
            address and (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10"))
        )
        if parsed.scheme == "http" and not private:
            raise ValueError("HTTP 仅用于回环或 Tailscale 地址；公网接口必须使用 HTTPS。")
        self.root = Path(root) / "siyuan_media"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scope = hashlib.sha256((base + "\n" + key).encode()).hexdigest()
        self.http = httpx.Client(base_url=base + "/", headers={"Authorization": "Bearer " + key},
                                 trust_env=False, follow_redirects=False, timeout=60, transport=transport)

    def close(self):
        self.http.close()

    def json(self, method, path, **kwargs):
        response = self.http.request(method, path, **kwargs)
        if response.is_error:
            try:
                code = response.json().get("error", {}).get("code", "media_request_failed")
            except (ValueError, AttributeError):
                code = "media_request_failed"
            raise RuntimeError(f"媒体接口拒绝请求：HTTP {response.status_code}，{code}。")
        return response.json()

    def video_options(self):
        return self.json("GET", "media/options", timeout=5)

    @staticmethod
    def video_capabilities(options):
        capabilities = options.get("single_generation", {}).get("video_capabilities")
        if not isinstance(capabilities, list):
            return None  # Older servers still enforce capability at submission.
        return [item for item in capabilities if isinstance(item, dict)
                and item.get("mode") in {"t2v", "i2v"}
                and type(item.get("duration")) is int and 4 <= item["duration"] <= 15
                and item.get("aspect_ratio") in {"16:9", "9:16"}]

    @classmethod
    def require_video_capability(cls, options, model, mode, duration, aspect_ratio):
        models = options.get("models")
        if isinstance(models, list) and model not in models:
            raise ValueError("当前账号未开放所选视频模型。")
        capabilities = cls.video_capabilities(options)
        if capabilities is not None and not any(
                item["mode"] == mode and item["duration"] == duration
                and item["aspect_ratio"] == aspect_ratio for item in capabilities):
            raise ValueError("该视频模式、时长和画幅组合尚未验收开放，请按 API 能力选项调整。")

    def generate(self, kind, body, request_id, *, reference=None, check_interrupt=lambda: None,
                 timeout_seconds=14400, poll_seconds=3):
        if kind not in {"image", "video"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("请填写唯一 request_id；重试保持原值，新作品换一个值。")
        digest = hashlib.sha256(json.dumps({"kind": kind, "body": body,
            "reference": hashlib.sha256(reference).hexdigest() if reference else None}, sort_keys=True).encode()).hexdigest()
        receipt = self.root / (hashlib.sha256((self.scope + kind + request_id).encode()).hexdigest() + ".json")
        job = None
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved["digest"] != digest:
                raise ValueError("request_id 已用于不同参数；新作品请换一个值。")
            job = saved.get("job")
        check_interrupt()
        if not job:
            if kind == "video":
                try:
                    options = self.video_options()
                except (httpx.HTTPError, RuntimeError, ValueError):
                    options = None  # The server remains the final admission check.
                if options is not None:
                    self.require_video_capability(options, body.get("model", "siyuan-video"),
                                                  body.get("mode", "i2v" if reference else "t2v"),
                                                  body.get("duration", 4), body.get("aspect_ratio", "16:9"))
            headers = {"Idempotency-Key": request_id, "Prefer": "respond-async"}
            if kind == "video":
                files = {"first_frame": ("first_frame.png", reference, "image/png")} if reference else {}
                # An empty file part list otherwise becomes form-urlencoded.
                multipart = [(name, (None, str(value))) for name, value in body.items()]
                multipart += list(files.items())
                job = self.json("POST", "videos", headers=headers, files=multipart)
            elif reference:
                job = self.json("POST", "images/edits", headers=headers, data=body,
                                files={"image": ("reference.png", reference, "image/png")})
            else:
                job = self.json("POST", "images/generations", headers=headers, json=body)
            if not re.fullmatch(r"(?:img|vid)_[A-Za-z0-9_-]+", str(job.get("id", ""))):
                raise RuntimeError("服务端未返回稳定任务编号；重试时必须沿用 request_id。")
            pending = receipt.with_suffix("." + uuid4().hex + ".pending")
            pending.write_text(json.dumps({"digest": digest, "job": {"id": job["id"]}}))
            pending.chmod(0o600)
            pending.replace(receipt)
        identifier = job["id"]
        if not re.fullmatch(r"(?:img|vid)_[A-Za-z0-9_-]+", identifier):
            raise ValueError("本地任务回执无效。")
        path = ("images/" if kind == "image" else "videos/") + identifier
        deadline = time.monotonic() + timeout_seconds
        while True:
            # Stopping a local graph detaches; it never generates a new remote job.
            check_interrupt()
            job = self.json("GET", path)
            if job["status"] == "completed":
                break
            if job["status"] in {"failed", "cancelled"}:
                raise RuntimeError(f"任务 {identifier} 已{job['status']}；请查看服务端任务记录。")
            if isinstance(job.get("error"), dict) and job["error"].get("code") == "media_recovery_required":
                raise RuntimeError(f"任务 {identifier} 需要管理员核对原执行记录；请保留 request_id，勿重新生成。")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"任务 {identifier} 仍在后台；保持 request_id 重新执行可继续等待。")
            time.sleep(poll_seconds)
        destination = self.root / (identifier + (".png" if kind == "image" else ".mp4"))
        pending = destination.with_suffix("." + uuid4().hex + ".part")
        size = 0
        try:
            with self.http.stream("GET", path + "/content") as response:
                response.raise_for_status()
                expected = {"image/png", "image/jpeg", "image/webp"} if kind == "image" else {"video/mp4"}
                content_type = response.headers.get("content-type", "").split(";")[0]
                if content_type not in expected:
                    raise RuntimeError("服务端返回了不支持的媒体格式。")
                destination = destination.with_suffix({"image/png": ".png", "image/jpeg": ".jpg",
                                                       "image/webp": ".webp", "video/mp4": ".mp4"}[content_type])
                with pending.open("wb") as output:
                    for chunk in response.iter_bytes():
                        check_interrupt()
                        size += len(chunk)
                        if size > (32 if kind == "image" else 512) * 1024 * 1024:
                            raise RuntimeError("媒体文件超过下载上限。")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if not size:
                raise RuntimeError("服务端返回了空媒体文件。")
            pending.chmod(0o600)
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)
        return destination, identifier
