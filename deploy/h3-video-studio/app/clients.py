from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .config import SETTINGS, load_minimax_key


RETRYABLE_HTTP = {429, 500, 502, 503, 504, 529}
FAILED_TASK_STATUSES = {"failed", "cancelled", "expired"}
MAX_REQUEST_BYTES = 64 * 1024 * 1024

ProgressCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> dict:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            if error.code in RETRYABLE_HTTP and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {error.code}: {raw}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
                continue
    raise RuntimeError(f"请求失败：{last_error}")


def _download(url: str, destination: Path, headers: dict[str, str] | None = None) -> None:
    request = urllib.request.Request(url, headers=headers or {})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=300) as response:
        with destination.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)


class ComfyClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or SETTINGS.comfy_url).rstrip("/")

    def health(self) -> dict:
        return _request_json("GET", f"{self.base_url}/system_stats", timeout=10)

    def free(self) -> None:
        try:
            _request_json(
                "POST",
                f"{self.base_url}/free",
                payload={"unload_models": True, "free_memory": True},
                timeout=30,
            )
        except Exception:
            pass

    def submit(self, workflow: dict) -> str:
        response = _request_json(
            "POST",
            f"{self.base_url}/prompt",
            payload={"prompt": workflow},
        )
        prompt_id = str(response.get("prompt_id", "")).strip()
        if not prompt_id:
            raise RuntimeError(f"ComfyUI 没有返回 prompt_id：{response}")
        return prompt_id

    def interrupt(self) -> None:
        _request_json("POST", f"{self.base_url}/interrupt", payload={})

    def prompt_state(self, prompt_id: str) -> str:
        history = _request_json(
            "GET",
            f"{self.base_url}/history/{urllib.parse.quote(prompt_id)}",
            timeout=30,
        )
        record = history.get(prompt_id)
        if record:
            status = record.get("status", {})
            if status.get("status_str") == "error":
                return "error"
            if status.get("completed") or self._find_video_output(record):
                return "completed"
        queue = _request_json("GET", f"{self.base_url}/queue", timeout=30)
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key, []):
                if len(item) > 1 and str(item[1]) == prompt_id:
                    return "active"
        return "missing"

    def wait_for_output(
        self,
        prompt_id: str,
        destination: Path,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        expected_seconds: int,
        timeout_seconds: int = 10800,
    ) -> dict:
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            if cancelled():
                self.interrupt()
                raise RuntimeError("任务已取消。")
            history = _request_json(
                "GET",
                f"{self.base_url}/history/{urllib.parse.quote(prompt_id)}",
                timeout=30,
            )
            record = history.get(prompt_id)
            elapsed = time.monotonic() - started
            progress(
                {
                    "progress": min(94, 5 + int(elapsed / max(expected_seconds, 1) * 88)),
                    "detail": f"ComfyUI 正在生成，已运行 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒",
                }
            )
            if record:
                status = record.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    raise RuntimeError(f"ComfyUI 工作流执行失败：{messages[-1:]}")
                output = self._find_video_output(record)
                if output:
                    query = urllib.parse.urlencode(output)
                    _download(f"{self.base_url}/view?{query}", destination)
                    return {
                        "prompt_id": prompt_id,
                        "comfy_output": output,
                        "elapsed_seconds": round(elapsed, 1),
                    }
            time.sleep(3)
        raise TimeoutError(f"ComfyUI 任务 {prompt_id} 超过 {timeout_seconds} 秒。")

    @staticmethod
    def _find_video_output(record: dict) -> dict | None:
        outputs = record.get("outputs", {})
        for output in outputs.values():
            for key in ("images", "videos"):
                for item in output.get(key, []):
                    filename = str(item.get("filename", ""))
                    if filename.lower().endswith((".mp4", ".webm", ".mov")):
                        return {
                            "filename": filename,
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                        }
        return None


class MiniMaxClient:
    def __init__(self, api_base: str | None = None) -> None:
        self.api_base = (api_base or SETTINGS.minimax_api_base).rstrip("/")

    def configured(self) -> bool:
        return bool(load_minimax_key())

    def context_ir(
        self,
        project: dict,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict:
        payload = {
            "model": "MiniMax-H3",
            "content": build_multimodal_content(project, project["prompt_original"], for_ir=True),
            "duration": int(project["duration"]),
            "ratio": "16:9",
        }
        result = self._create_and_poll(
            "/v2/h3_context_ir",
            payload,
            progress=progress,
            cancelled=cancelled,
            label="Context IR",
            timeout_seconds=900,
        )
        content = result["task"].get("content", {})
        prompt = content.get("prompt") if isinstance(content, dict) else None
        if not prompt:
            raise RuntimeError(f"Context IR 成功但没有返回优化提示词：{result}")
        return {
            "task_id": str(result["task"].get("id", "")),
            "prompt": prompt,
            "usage": result["task"].get("usage"),
        }

    def generate_768(
        self,
        project: dict,
        destination: Path,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict:
        payload = {
            "model": "MiniMax-H3",
            "content": build_multimodal_content(
                project,
                project["prompt_approved"],
                for_ir=False,
            ),
            "duration": int(project["duration"]),
            "resolution": "768P",
            "ratio": "16:9",
            "aigc_watermark": bool(project.get("watermark", False)),
        }
        result = self._create_and_poll(
            "/v2/video_generation",
            payload,
            progress=progress,
            cancelled=cancelled,
            label="官方 768P",
            timeout_seconds=3600,
        )
        task = result["task"]
        url = task.get("content", {}).get("url")
        if not url:
            raise RuntimeError(f"官方 768P 成功但没有返回视频：{result}")
        _download(url, destination)
        return {"task_id": str(task.get("id", ""))}

    def regenerate_2k(
        self,
        project: dict,
        source: Path,
        destination: Path,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> dict:
        video_uri = _data_uri(source, "video/mp4")
        payload = {
            "model": "MiniMax-H3",
            "content": [
                {"type": "text", "text": project["prompt_approved"]},
                {
                    "type": "video_url",
                    "video_url": {"url": video_uri},
                    "role": "base_video",
                },
            ],
            "resolution": "2K",
            "aigc_watermark": bool(project.get("watermark", False)),
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("2K 请求超过 64MB，请先降低 768P 文件码率。")
        result = self._create_and_poll(
            "/v2/video_regeneration",
            payload,
            progress=progress,
            cancelled=cancelled,
            label="官方 2K",
            timeout_seconds=3600,
        )
        task = result["task"]
        url = task.get("content", {}).get("url")
        if not url:
            raise RuntimeError(f"官方 2K 成功但没有返回视频：{result}")
        _download(url, destination)
        return {"task_id": str(task.get("id", ""))}

    def _create_and_poll(
        self,
        path: str,
        payload: dict,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        label: str,
        timeout_seconds: int,
    ) -> dict:
        key = load_minimax_key()
        if not key:
            raise RuntimeError("MINIMAX_API_KEY 未配置。")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError(f"{label} 请求超过 64MB。")
        headers = {"Authorization": f"Bearer {key}"}
        created = _request_json(
            "POST",
            f"{self.api_base}{path}",
            payload=payload,
            headers=headers,
        )
        task_id = str(created.get("task_id", "")).strip()
        if not task_id:
            raise RuntimeError(f"{label} 没有返回 task_id：{created}")
        progress({"progress": 8, "remote_task_id": task_id, "detail": f"{label} 已提交"})

        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            if cancelled():
                raise RuntimeError("任务已取消。")
            result = _request_json(
                "GET",
                f"{self.api_base}/v2/query/video_generation/{urllib.parse.quote(task_id)}",
                headers=headers,
                timeout=60,
            )
            task = result.get("task")
            if not isinstance(task, dict):
                raise RuntimeError(f"{label} 返回格式异常：{result}")
            status = str(task.get("status", "")).lower()
            elapsed = time.monotonic() - started
            progress(
                {
                    "progress": min(94, 10 + int(elapsed / max(timeout_seconds / 4, 1) * 75)),
                    "remote_task_id": task_id,
                    "detail": f"{label}：{status or '排队中'}",
                }
            )
            if status == "succeeded":
                return result
            if status in FAILED_TASK_STATUSES:
                raise RuntimeError(f"{label} 失败：{task.get('error')}")
            time.sleep(5 if label == "Context IR" else 15)
        raise TimeoutError(f"{label} 任务 {task_id} 超时。")


def build_multimodal_content(project: dict, prompt: str, *, for_ir: bool) -> list[dict]:
    assets = project.get("assets", {})
    mode = project["mode"]
    content: list[dict] = [{"type": "text", "text": prompt}]

    if mode in {"i2v", "fl2v", "hybrid"}:
        content.append(_asset_content(assets["first_frame"], "image_url", "first_frame"))
    if mode in {"l2v", "fl2v", "hybrid"}:
        content.append(_asset_content(assets["last_frame"], "image_url", "last_frame"))
    if mode == "reference":
        if "reference_image" in assets:
            content.append(
                _asset_content(assets["reference_image"], "image_url", "reference_image")
            )
        if "reference_video" in assets:
            content.append(
                _asset_content(assets["reference_video"], "video_url", "reference_video")
            )
        if "reference_audio" in assets and "reference_video" not in assets:
            content.append(
                _asset_content(assets["reference_audio"], "audio_url", "reference_audio")
            )

    # Official Context IR forbids mixing first/last-frame roles with reference roles.
    # Hybrid and source-audio locking therefore optimize the endpoint frames first;
    # their extra identity/audio assets remain part of local ComfyUI generation.
    if not for_ir and project.get("audio_policy") == "reference" and "reference_audio" in assets:
        content.append(
            _asset_content(assets["reference_audio"], "audio_url", "reference_audio")
        )
    return content


def _asset_content(asset: dict, media_type: str, role: str) -> dict:
    path = Path(asset.get("ir_path") or asset["path"])
    mime = asset.get("mime") or mimetypes.guess_type(path.name)[0]
    if not mime:
        mime = {
            "image_url": "image/png",
            "video_url": "video/mp4",
            "audio_url": "audio/mp4",
        }[media_type]
    return {
        "type": media_type,
        media_type: {"url": _data_uri(path, mime)},
        "role": role,
    }


def _data_uri(path: Path, mime: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
