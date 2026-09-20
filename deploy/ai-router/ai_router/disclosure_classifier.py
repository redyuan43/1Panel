"""Binary classifier for identity disclosure detection.

Two-stage system replacing the pure-regex gate in identity.py:
1. Structured feature extraction (zero-latency; regex feeds FEATURES, not verdicts)
2. Logistic regression over those features (<0.5ms, pure numpy)
3. Evidence gate + regex fallback for borderline cases

No ML dependency at runtime — numpy only.

DESIGN NOTE (pattern-count coupling)
------------------------------------
The previous revision hard-coded `pat_0` .. `pat_6` as dataclass fields and
scanned `range(7)`. identity.py actually defines EIGHT disclosure patterns, so
the last one (`your underlying model` style English asks) was silently ignored
by both the feature vector and the evidence gate — a real recall hole.

This revision stores pattern hits as a variable-length tuple, so the feature
vector adapts to however many patterns identity.py defines. If the count changes
the model file must be retrained; `predict()` detects a dimension mismatch and
degrades to the regex verdict instead of raising.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Attack-surface patterns beyond the disclosure patterns defined in identity.py
# ---------------------------------------------------------------------------

_ENCODE_REQUEST_PATTERN = re.compile(
    r'(base64|hex|encode|decode|编码|解码|十六进制|morse|unicode)',
    re.IGNORECASE,
)
_ROLEPLAY_PATTERN = re.compile(
    r'(假装|扮演|act\s+as|pretend|on[-\s]call|admin|值班|管理员|auditor|审计|'
    r'internal\s+engineer|on-call\s+operator)',
    re.IGNORECASE,
)
_MULTI_TURN_PATTERN = re.compile(
    r'(上一轮|之前|刚才|previously|last\s+turn|earlier|previous|'
    r'你之前|你刚才|前一轮)',
    re.IGNORECASE,
)
_INJECTION_PATTERN = re.compile(
    r'(<system>|<developer>|SYSTEM:|fake\s+system|higher\s+priority|'
    r'bypass|ignore\s+(all|previous)|disregard)',
    re.IGNORECASE,
)
_MIXED_TASK_PATTERN = re.compile(
    r'["\'`\u201c\u201d\u2018\u2019<>]|\n|翻译|比较|举例|解释|推荐|如何|为什么|'
    r'顺便|另外|然后帮|translate|compare|explain|example',
    re.IGNORECASE,
)
_QUOTE_MARKER_PATTERN = re.compile(r'["\'`\u201c\u201d\u2018\u2019<>]')


@dataclass(frozen=True)
class DisclosureFeatures:
    """Feature vector for disclosure classification.

    Dimension = 14 fixed features + one bit per disclosure pattern passed in.
    Kept variable-length so identity.py can add or remove regexes without
    touching this class.
    """
    text_length_norm: float     # len(text) / 240, capped at 1
    has_quote: float            # contains quote/code markers
    has_newline: float          # contains a newline
    has_mixed_task: float       # mixed-task keywords (翻译/compare/explain/...)
    is_chinese: float           # CJK ratio > 0.3
    is_english: float           # ASCII-alpha ratio > 0.5
    has_tool_choice: float      # request pins tool_choice
    has_response_format: float  # request pins response_format
    identity_context: float     # prior turn established an identity topic
    followup_match: float       # follow-up pattern hit (needs identity_context)
    encode_request: float       # base64 / hex / encode keywords
    roleplay: float             # role-play, admin, auditor framing
    multi_turn: float           # references a previous turn
    injection: float            # prompt-injection markers
    pattern_hits: tuple[float, ...]  # one bit per identity.py disclosure pattern

    @property
    def any_pattern_hit(self) -> bool:
        return any(self.pattern_hits)

    @property
    def any_attack_surface_hit(self) -> bool:
        return bool(
            any(self.pattern_hits)
            or self.followup_match
            or self.encode_request
            or self.roleplay
            or self.multi_turn
            or self.injection
        )

    def to_vector(self) -> np.ndarray:
        return np.array([
            self.text_length_norm,
            self.has_quote, self.has_newline, self.has_mixed_task,
            self.is_chinese, self.is_english,
            self.has_tool_choice, self.has_response_format,
            self.identity_context,
            self.followup_match,
            self.encode_request, self.roleplay,
            self.multi_turn, self.injection,
            *self.pattern_hits,
        ], dtype=np.float32)


class DisclosureClassifier:
    """Logistic-regression classifier with an evidence gate and regex fallback.

    Runtime contract: loads a .npz holding `weights` + `bias`. If the file is
    missing, malformed, non-finite, or dimensionally stale, the classifier
    silently degrades to the regex verdict — model artifacts must never break
    the serving path.
    """

    THRESHOLD_HIGH = 0.7
    THRESHOLD_LOW = 0.3

    # Expected width of a shipped model file: 14 fixed features + one bit per
    # disclosure pattern that identity.py currently defines (8) = 22.
    #
    # Kept as a class constant because callers (tests, training/eval scripts)
    # allocate weight vectors with it before any model is loaded, and because
    # computing it from identity.py here would be a circular import.
    # extract_features() still sizes the vector from the `patterns` argument;
    # retrain and update this schema width whenever the pattern count changes.
    DIM = 22

    def __init__(self, model_path: Path | None = None):
        self._weights: np.ndarray | None = None
        self._bias: float = 0.0
        self._dimension_mismatch = False
        if model_path is not None and model_path.exists():
            self._load(model_path)

    def _load(self, path: Path) -> None:
        try:
            with np.load(path, allow_pickle=False) as data:
                weights = np.asarray(data["weights"], dtype=np.float32)
                bias = np.asarray(data["bias"], dtype=np.float32)
        except (OSError, ValueError, KeyError, TypeError):
            return
        if (
            weights.ndim != 1
            or bias.shape != ()
            or not np.isfinite(weights).all()
            or not np.isfinite(bias).all()
        ):
            return
        self._weights = weights
        self._bias = float(bias)
        self._dimension_mismatch = weights.shape[0] != self.DIM

    @property
    def has_model(self) -> bool:
        """True only when a usable, dimension-matched model is loaded."""
        return self._weights is not None and not self._dimension_mismatch

    @property
    def dimension(self) -> int:
        return 0 if self._weights is None else int(self._weights.shape[0])

    def predict(self, features: DisclosureFeatures) -> tuple[bool, float]:
        """Return (is_disclosure, confidence).

        Decision order is load-bearing:

        1. EVIDENCE GATE. Zero attack surface -> False. Fixes the observed 27%
           false-positive rate: with a trained bias around +1.3, a query
           carrying ZERO evidence was still pushed past 0.5 by the bias alone
           (e.g. "什么是 GPU 量化？").

        2. REGEX SHORT CIRCUIT. A direct disclosure or contextual follow-up
           remains sensitive even when quoted, split across lines, embedded in
           another task, or padded. Formatting must never turn a positive into
           an allow decision.

        3. NORMAL-TASK VETO for weak, non-regex evidence, then LR arbitration.
        """
        # 1. Evidence gate.
        if not features.any_attack_surface_hit:
            return False, 0.0

        # 2. A direct regex hit is deterministic policy evidence. Do not let
        # quote/newline/mixed-task formatting downgrade it to an allow result.
        if features.any_pattern_hit or features.followup_match:
            return True, 0.95

        # 3. Formatting exclusions apply only to weak attack-surface signals
        # that do not contain a direct disclosure or follow-up pattern.
        if features.has_quote or features.has_newline or features.has_mixed_task:
            return False, 0.0

        x = features.to_vector()
        if self._weights is not None:
            if x.shape[0] != self._weights.shape[0]:
                # Stale model for the current pattern set: fall back, don't fail.
                self._dimension_mismatch = True
            else:
                logit = float(x @ self._weights + self._bias)
                prob = 1.0 / (1.0 + np.exp(-np.clip(logit, -500, 500)))
                if prob > self.THRESHOLD_HIGH:
                    return True, prob
                if prob < self.THRESHOLD_LOW:
                    return False, prob
                # Borderline: fall through to the regex verdict.

        regex_hit = any(features.pattern_hits) or bool(features.followup_match)
        return bool(regex_hit), 0.5 if regex_hit else 0.0

    def classify(
        self,
        text: str,
        *,
        identity_context: bool = False,
        has_tool_choice: bool = False,
        has_response_format: bool = False,
        patterns: tuple[re.Pattern, ...] = (),
        followup_patterns: tuple[re.Pattern, ...] = (),
    ) -> tuple[bool, float]:
        """Extract features and classify in one call.

        This is the entry point identity.py uses. Kept as a thin wrapper so
        callers that only want a verdict do not have to know about the
        two-stage internals, while tests and diagnostics can still drive
        extract_features() and predict() separately.

        Input size is bounded by privacy_view before this method is called.
        Length is a feature, never an allow condition: padding a disclosure
        request must not bypass the deterministic patterns.
        """
        if not text:
            return False, 0.0
        return self.predict(self.extract_features(
            text,
            identity_context=identity_context,
            has_tool_choice=has_tool_choice,
            has_response_format=has_response_format,
            patterns=patterns,
            followup_patterns=followup_patterns,
        ))

    def extract_features(
        self,
        text: str,
        *,
        identity_context: bool = False,
        has_tool_choice: bool = False,
        has_response_format: bool = False,
        patterns: tuple[re.Pattern, ...] = (),
        followup_patterns: tuple[re.Pattern, ...] = (),
    ) -> DisclosureFeatures:
        """Build the feature vector for one query.

        `patterns` length determines the vector dimension — no fixed cap.
        """
        length = len(text)
        cjk_count = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        ascii_alpha = sum(1 for ch in text if ch.isascii() and ch.isalpha())

        pattern_hits = tuple(
            1.0 if pattern.search(text) else 0.0 for pattern in patterns
        )

        followup = 0.0
        if identity_context and length <= 160:
            for pattern in followup_patterns:
                if pattern.search(text):
                    followup = 1.0
                    break

        return DisclosureFeatures(
            text_length_norm=min(length / 240.0, 1.0),
            has_quote=1.0 if _QUOTE_MARKER_PATTERN.search(text) else 0.0,
            has_newline=1.0 if '\n' in text else 0.0,
            has_mixed_task=1.0 if _MIXED_TASK_PATTERN.search(text) else 0.0,
            is_chinese=1.0 if (length > 0 and cjk_count / max(length, 1) > 0.3) else 0.0,
            is_english=1.0 if (length > 0 and ascii_alpha / max(length, 1) > 0.5) else 0.0,
            has_tool_choice=1.0 if has_tool_choice else 0.0,
            has_response_format=1.0 if has_response_format else 0.0,
            identity_context=1.0 if identity_context else 0.0,
            followup_match=followup,
            encode_request=1.0 if _ENCODE_REQUEST_PATTERN.search(text) else 0.0,
            roleplay=1.0 if _ROLEPLAY_PATTERN.search(text) else 0.0,
            multi_turn=1.0 if _MULTI_TURN_PATTERN.search(text) else 0.0,
            injection=1.0 if _INJECTION_PATTERN.search(text) else 0.0,
            pattern_hits=pattern_hits,
        )
