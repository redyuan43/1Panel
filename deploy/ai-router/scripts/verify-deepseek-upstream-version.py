#!/usr/bin/env python3
"""只读验证 DeepSeek 上游真实模型版本。

背景：registry 中 `cloud-deepseek-v4-flash` 的 provider_model 仍是
`deepseek-v4-flash`。官方声明该旧 ID 已被 DeepSeek-V4.1-Flash 承接，
但厂商侧的静默路由无法从本地配置推断，必须用上游自报字段和视觉能力
判别取得证据。

本脚本只做两件事：
1. 直连 api.deepseek.com，对比旧 ID / 新 ID / pro ID 的响应自报字段。
2. 用旧 ID 发起一次真实图片请求，判断上游是否已具备视觉能力。

不修改任何配置，不写入 Router 状态，只发起少量真实推理请求（有极小的
计费成本）。凭据仅从 router.env 读取，不打印明文。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib

API_BASE = "https://api.deepseek.com"
ENV_PATH = "/opt/1panel/ai-router/router.env"
KEY_NAME = "AI_ROUTER_DEEPSEEK_API_KEY"
TEXT_IDS = ("deepseek-v4-flash", "deepseek-flash", "deepseek-v4-pro")
IMAGE_IDS = ("deepseek-v4-flash", "deepseek-flash")
TIMEOUT_SECONDS = 120


def load_api_key(path: str, name: str) -> str:
    if os.environ.get(name):
        return str(os.environ[name])
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except OSError as exc:
        raise SystemExit(f"无法读取凭据文件 {path}: {exc}") from None
    raise SystemExit(f"凭据文件 {path} 中未找到 {name}")


def call(
    path: str,
    api_key: str,
    payload: dict | None = None,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": {"message": raw[:400]}}
    except Exception as exc:  # noqa: BLE001 - 网络异常要如实上报
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png(width: int = 128, height: int = 128) -> bytes:
    """蓝边、左上象限红色的识别图，用于视觉能力判别。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if x < 4 or y < 4 or x >= width - 4 or y >= height - 4:
                rows += bytes((0, 0, 255))
            elif x < width // 2 and y < height // 2:
                rows += bytes((255, 0, 0))
            else:
                rows += bytes((255, 255, 255))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows)))
        + _chunk(b"IEND", b"")
    )


def summarize(status: int, payload: dict) -> dict:
    error = payload.get("error")
    message = ""
    if isinstance(error, dict):
        message = str(error.get("message", ""))[:300]
    content = ""
    reasoning = ""
    finish_reason = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = str(first.get("finish_reason", ""))
        message_value = first.get("message")
        if isinstance(message_value, dict):
            content = str(message_value.get("content") or "")[:200]
            reasoning = str(message_value.get("reasoning_content") or "")[:200]
    return {
        "http_status": status,
        "reported_model": payload.get("model"),
        "system_fingerprint": payload.get("system_fingerprint"),
        "finish_reason": finish_reason,
        "content": content,
        "reasoning_content": reasoning,
        "error": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="", help="证据 JSON 落盘路径")
    args = parser.parse_args()

    api_key = load_api_key(ENV_PATH, KEY_NAME)
    report: dict = {
        "checked_at": __import__("datetime").datetime.now().isoformat(
            timespec="seconds"
        ),
        "api_base": API_BASE,
        "key_source": ENV_PATH,
        "models_endpoint": {},
        "text_probes": {},
        "image_probes": {},
    }

    status, payload = call("/models", api_key)
    ids = [
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict)
    ]
    report["models_endpoint"] = {
        "http_status": status,
        "ids": ids,
        "has_legacy_id": "deepseek-v4-flash" in ids,
        "has_new_id": "deepseek-flash" in ids,
        "error": payload.get("error"),
    }

    print("=== GET /models ===")
    print(f"status={status} ids={ids or '(none)'}")

    print("\n=== 文本探针：响应自报字段 ===")
    for model_id in TEXT_IDS:
        status, payload = call(
            "/chat/completions",
            api_key,
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "reply with: pong"}],
                "max_tokens": 16,
                "temperature": 0,
            },
        )
        report["text_probes"][model_id] = summarize(status, payload)
        item = report["text_probes"][model_id]
        print(
            f"{model_id:22} status={item['http_status']:<4} "
            f"model={item['reported_model']!r} "
            f"fingerprint={item['system_fingerprint']!r} "
            f"error={item['error'][:80]!r}"
        )

    image_uri = "data:image/png;base64," + base64.b64encode(
        make_png()
    ).decode("ascii")
    question = "图中左上角象限是什么颜色？只回答颜色名称。"
    print("\n=== 图片探针：视觉能力判别 ===")
    for model_id in IMAGE_IDS:
        status, payload = call(
            "/chat/completions",
            api_key,
            {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_uri},
                            },
                        ],
                    }
                ],
                "max_tokens": 1024,
                "temperature": 0,
            },
        )
        report["image_probes"][model_id] = summarize(status, payload)
        item = report["image_probes"][model_id]
        print(
            f"{model_id:22} status={item['http_status']:<4} "
            f"model={item['reported_model']!r} "
            f"finish={item['finish_reason']!r} "
            f"content={item['content']!r} "
            f"error={item['error'][:100]!r}"
        )

    # 对照组：同一问题不带图片。若也答"红色"，说明视觉结论不成立。
    status, payload = call(
        "/chat/completions",
        api_key,
        {
            "model": IMAGE_IDS[0],
            "messages": [{"role": "user", "content": question}],
            "max_tokens": 1024,
            "temperature": 0,
        },
    )
    report["text_only_control"] = summarize(status, payload)
    item = report["text_only_control"]
    print(
        f"{'(对照) 无图片':22} status={item['http_status']:<4} "
        f"model={item['reported_model']!r} "
        f"content={item['content']!r} "
        f"error={item['error'][:100]!r}"
    )

    if args.out:
        directory = os.path.dirname(args.out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"\n证据已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
