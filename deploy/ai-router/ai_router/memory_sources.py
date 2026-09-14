"""Extract visible, sanitized evidence from archived requests, not routed recall."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from .compaction import TOOL_SUMMARY_PREFIX, extract_messages
from .memory_index import MemorySource


RECALL_MARKER = "<router-history-recall>"
CAPSULE_MARKERS = ("Conversation migration capsule.", "<compacted-summary>", RECALL_MARKER, TOOL_SUMMARY_PREFIX)
_SECRETS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"'<>}]+", re.I),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I),
    re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+", re.I),
)


def sanitize_evidence(text: str, forbidden_phrases: Iterable[str] = ()) -> str:
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    for phrase in forbidden_phrases:
        if phrase:
            text = text.replace(phrase, "[route directive removed]")
    return text


def visible_text(value) -> str:
    """Images, files and reasoning are not silently interpreted as visible text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (visible_text(item) for item in value)))
    if isinstance(value, dict) and value.get("type") in {"text", "input_text", "output_text"}:
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def visible_message(item: dict) -> tuple[str, str, str]:
    role = item.get("role", "")
    if item.get("type") == "function_call_output":
        role, text = "tool", visible_text(item.get("output"))
    elif role in {"user", "assistant", "tool"}:
        text = visible_text(item.get("content"))
    else:
        return "", "", ""
    if any(marker in text for marker in CAPSULE_MARKERS):
        return "", "", ""
    identity = json.dumps([role, text, item.get("tool_call_id", item.get("call_id", ""))],
                          ensure_ascii=False, separators=(",", ":"))
    return role, text, hashlib.sha256(identity.encode()).hexdigest()


def text_chunks(text: str, *, size: int = 2400, overlap: int = 200):
    """Keep line/paragraph boundaries where possible; preserve source offsets."""
    if size < 64 or not 0 <= overlap < size // 2:
        raise ValueError("invalid history chunk size/overlap")
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield start, text[start:end]
        if end == len(text):
            break
        start = end - overlap


def archived_sources(payload: dict, *, client_id: str, cloud_allowed: bool,
                     forbidden_phrases: Iterable[str] = (),
                     legacy_cloud_approved: bool = False) -> list[MemorySource]:
    request = payload.get("request", {})
    if request.get("client_id") != client_id:
        return []
    # Current consent cannot relax an original local-only restriction. Unknown
    # legacy provenance requires a separate explicit historical export approval.
    policy = request.get("history_source_policy")
    missing_policy = "history_source_policy" not in request
    source_unknown = missing_policy and cloud_allowed is True and legacy_cloud_approved is not True
    if isinstance(policy, dict) and type(policy.get("version")) is int and policy["version"] == 1:
        source_cloud = policy.get("local_only") is False
    elif missing_policy:
        source_cloud = legacy_cloud_approved is True
    else:
        source_cloud = False
    cloud_allowed = cloud_allowed is True and source_cloud
    conversation_id, request_id = request.get("conversation_id"), request.get("request_id")
    api_kind = request.get("protocol")
    if not conversation_id or not request_id or api_kind not in {"chat", "responses"}:
        return []
    # This field is captured after directive removal, before recall/normalization.
    body = request.get("received_body")
    if not isinstance(body, dict):
        return []
    items = extract_messages(body, api_kind)
    response = payload.get("response", {})
    if response.get("complete") is True and response.get("status_code") == 200:
        output = response.get("assistant_items")
        if isinstance(output, list):
            items.extend(output)
        elif isinstance(response.get("body"), dict):
            from .history import assistant_items_from_response
            response_body = response["body"]
            if response_body.get("encoding") == "json":
                response_body = response_body.get("value")
            if isinstance(response_body, dict):
                items.extend(assistant_items_from_response(json.dumps(response_body).encode(), api_kind))
    sources = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role, text, message_id = visible_message(item)
        if not text.strip():
            continue
        text = sanitize_evidence(text, forbidden_phrases)
        for offset, chunk in text_chunks(text):
            if not chunk.strip():
                continue
            sources.append(MemorySource(client_id=client_id, conversation_id=str(conversation_id),
                request_id=str(request_id), message_id=message_id, role=role, text=chunk,
                created_at=float(payload.get("received_at", 0)),
                cloud_allowed=cloud_allowed, offset=offset, cloud_unknown=source_unknown))
    return sources
