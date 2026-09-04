from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any


@dataclass(frozen=True)
class ReviewView:
    current_query: str
    context: str = ""
    source: str = "message"
    certain: bool = True


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(content_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(
            content_text(value[key])
            for key in ("text", "input_text", "content")
            if value.get(key) is not None
        )
    return ""


class _WorkBuddyView(HTMLParser):
    # These markers convey provenance, not permission to bypass model safeguards.
    containers = {
        "system-reminder", "previous_user_message", "previous_assistant_message",
        "previous_tool_call", "previous_tool_result", "conversation_history",
    }

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.queries: list[tuple[bool, list[str]]] = []
        self.active: tuple[bool, list[str]] | None = None
        self.invalid = False
        self.tail = ""
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "user_query":
            if self.active is not None:
                self.invalid = True
            self.active = (not self.stack, [])
        if tag in self.containers:
            self.stack.append(tag)
            if len(self.stack) > 128:
                raise ValueError("wrapper nesting limit")

    def handle_endtag(self, tag: str) -> None:
        if tag == "user_query":
            if self.active is None:
                self.invalid = True
            else:
                self.queries.append(self.active)
                self.active = None
                self.tail = ""
        if tag in self.containers:
            if not self.stack or self.stack[-1] != tag:
                self.invalid = True
            else:
                self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.active is not None:
            self.active[1].append(data)
        elif not self.stack:
            self.tail += data


def review_view(body: dict[str, Any], api_kind: str) -> ReviewView:
    if api_kind == "responses" and isinstance(body.get("input"), str):
        messages = [{"role": "user", "content": body["input"]}]
    else:
        messages = body.get("messages" if api_kind == "chat" else "input", [])
    if not isinstance(messages, list):
        return ReviewView("", source="missing", certain=False)
    users = [
        content_text(item.get("content"))
        for item in messages
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if not users:
        return ReviewView("", source="missing", certain=False)
    text = users[-1].strip()
    context = "\n".join(users[-3:-1])
    if len(text) > 262144:
        return ReviewView("", source="oversize", certain=False)
    if any(marker in text.lower() for marker in (
        "<user_query", "<system-reminder", "<previous_user_message",
    )):
        try:
            parsed = _WorkBuddyView(text)
        except (ValueError, RecursionError):
            return ReviewView("", source="ambiguous_wrapper", certain=False)
        current = ["".join(parts).strip() for root, parts in parsed.queries if root]
        if (
            parsed.invalid or parsed.stack or parsed.active is not None
            or len(current) != 1 or not current[0] or parsed.tail.strip()
            or not parsed.queries[-1][0]
            or not text.lower().endswith("</user_query>")
        ):
            return ReviewView("", source="ambiguous_wrapper", certain=False)
        previous = ["".join(parts) for root, parts in parsed.queries if not root]
        return ReviewView(current[0], "\n".join(previous[-2:]), "workbuddy")
    return ReviewView(text, context)
