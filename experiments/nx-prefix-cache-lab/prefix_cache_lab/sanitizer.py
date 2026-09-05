from __future__ import annotations

import re

from .util import digest_text


_PATTERNS = (
    (
        "authorization",
        re.compile(
            r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)"
            r"([A-Za-z0-9._~+/=-]{8,})"
        ),
    ),
    (
        "secret-field",
        re.compile(
            r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|'
            r'client[_-]?secret|password)["\']?\s*[:=]\s*["\']?)'
            r"([^\"'\s,}]{6,})"
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "aws-key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "email",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
)


def _replacement(kind: str, value: str) -> str:
    return f"<REDACTED:{kind}:{digest_text(value)[:12]}>"


def sanitize_text(value: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    sanitized = value
    for kind, pattern in _PATTERNS:
        def replace(match: re.Match[str]) -> str:
            counts[kind] = counts.get(kind, 0) + 1
            if match.lastindex:
                prefix = match.group(1)
                secret = match.group(match.lastindex)
                return prefix + _replacement(kind, secret)
            return _replacement(kind, match.group(0))

        sanitized = pattern.sub(replace, sanitized)
    sanitized = re.sub(
        r"(?i)C:\\Users\\Ivan",
        r"C:\\Users\\<REDACTED:user>",
        sanitized,
    )
    return sanitized, counts


def likely_contains_secret(value: str) -> bool:
    checks = (
        r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9]",
        r"(?i)(api[_-]?key|password|client[_-]?secret)\s*[:=]\s*[^\s<]",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    )
    return any(re.search(pattern, value) for pattern in checks)
