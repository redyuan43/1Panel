"""Binary classifier for identity disclosure detection.

Replaces the pure-regex approach in identity.py with a two-stage system:
1. Structured feature extraction with conservative normal-task exclusions
2. Logistic regression on 21-dim features (pure numpy)
3. Fallback to existing regex for borderline cases

No external ML dependencies at runtime — only numpy.
Training uses a standalone script (scripts/train_disclosure_classifier.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Feature patterns (beyond the 7 existing _IDENTITY_DISCLOSURE_PATTERNS)
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


@dataclass(frozen=True)
class DisclosureFeatures:
    """21-dimensional feature vector for disclosure classification."""
    text_length_norm: float   # len(text) / 240
    has_quote: float          # contains quote/code markers
    has_newline: float        # contains \n
    has_mixed_task: float     # mixed-task keywords
    is_chinese: float         # CJK char ratio > 0.3
    is_english: float         # ASCII alpha ratio > 0.5
    has_tool_choice: float    # body has tool_choice
    has_response_format: float  # body has response_format
    identity_context: float   # multi-turn identity context flag
    pat_0: float              # existing pattern 0 match
    pat_1: float              # existing pattern 1 match
    pat_2: float              # existing pattern 2 match
    pat_3: float              # existing pattern 3 match
    pat_4: float              # existing pattern 4 match
    pat_5: float              # existing pattern 5 match
    pat_6: float              # existing pattern 6 match
    followup_match: float     # followup pattern match
    # --- new attack-surface features ---
    encode_request: float     # base64/hex/encode keywords
    roleplay: float           # role-play / admin / auditor
    multi_turn: float         # previous-turn reference
    injection: float          # prompt injection markers

    def to_vector(self) -> np.ndarray:
        return np.array([
            self.text_length_norm,
            self.has_quote, self.has_newline, self.has_mixed_task,
            self.is_chinese, self.is_english,
            self.has_tool_choice, self.has_response_format,
            self.identity_context,
            self.pat_0, self.pat_1, self.pat_2, self.pat_3,
            self.pat_4, self.pat_5, self.pat_6,
            self.followup_match,
            self.encode_request, self.roleplay,
            self.multi_turn, self.injection,
        ], dtype=np.float32)


class DisclosureClassifier:
    """Lightweight LR classifier with regex fallback.

    At runtime: loads a .npz file containing weights + bias.
    If no model file exists, falls back to pure-regex decision (current behavior).
    """

    DIM = 21  # number of features

    def __init__(self, model_path: Path | None = None):
        self._weights: np.ndarray | None = None
        self._bias: float = 0.0
        if model_path is not None and model_path.exists():
            self._load(model_path)

    def _load(self, path: Path) -> None:
        data = np.load(path)
        self._weights = data["weights"].astype(np.float32)
        self._bias = float(data["bias"])

    @property
    def has_model(self) -> bool:
        return self._weights is not None

    def classify(self, text: str, **feature_options) -> tuple[bool, float]:
        """Shared runtime/evaluation path for an extracted current query."""
        if not text or len(text) > 240:
            return False, 0.0
        return self.predict(self.extract_features(text, **feature_options))

    def predict(self, features: DisclosureFeatures) -> tuple[bool, float]:
        """Returns (is_disclosure, confidence).

        Decision logic (evidence-gated):
        - NO evidence (zero regex/feature hits): always False — short-circuit.
          Queries without evidence must not become positive from bias alone.
        - With evidence: LR scores the combination.
          p > threshold_high (0.7): True
          p < threshold_low  (0.3): False
          Otherwise: fall back to the regex verdict
        """
        # Preserve normal-task exclusions even without weights or at a high score.
        # A False result continues normal model handling; it is not authorization.
        if features.has_quote or features.has_newline or features.has_mixed_task:
            return False, 0.0

        # Evidence gate: any pattern/followup/attack-surface hit counts as evidence.
        has_evidence = (
            any(getattr(features, f"pat_{i}") for i in range(7))
            or features.followup_match
            or features.encode_request
            or features.roleplay
            or features.multi_turn
            or features.injection
        )
        if not has_evidence:
            return False, 0.0

        if self._weights is not None:
            x = features.to_vector()
            logit = float(x @ self._weights + self._bias)
            prob = 1.0 / (1.0 + np.exp(-np.clip(logit, -500, 500)))
            if prob > 0.7:
                return True, prob
            if prob < 0.3:
                return False, prob
            # Borderline: fall through to regex
        # Fallback: any existing pattern match -> True
        regex_hit = (
            any(getattr(features, f"pat_{i}") for i in range(7))
            or features.followup_match
        )
        return bool(regex_hit), 0.5 if regex_hit else 0.0

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
        """Extract 21-dim feature vector from raw text + request metadata."""
        length = len(text)
        cjk_count = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        ascii_alpha = sum(1 for ch in text if ch.isascii() and ch.isalpha())

        pat_flags = []
        for p in patterns:
            pat_flags.append(1.0 if p.search(text) else 0.0)
        # Pad to 7 if fewer patterns provided
        while len(pat_flags) < 7:
            pat_flags.append(0.0)

        followup = 0.0
        if identity_context and length <= 160:
            for p in followup_patterns:
                if p.search(text):
                    followup = 1.0
                    break

        return DisclosureFeatures(
            text_length_norm=min(length / 240.0, 1.0),
            has_quote=1.0 if re.search(r'["\'`\u201c\u201d\u2018\u2019<>]', text) else 0.0,
            has_newline=1.0 if '\n' in text else 0.0,
            has_mixed_task=1.0 if _MIXED_TASK_PATTERN.search(text) else 0.0,
            is_chinese=1.0 if (length > 0 and cjk_count / max(length, 1) > 0.3) else 0.0,
            is_english=1.0 if (length > 0 and ascii_alpha / max(length, 1) > 0.5) else 0.0,
            has_tool_choice=1.0 if has_tool_choice else 0.0,
            has_response_format=1.0 if has_response_format else 0.0,
            identity_context=1.0 if identity_context else 0.0,
            pat_0=pat_flags[0], pat_1=pat_flags[1], pat_2=pat_flags[2],
            pat_3=pat_flags[3], pat_4=pat_flags[4], pat_5=pat_flags[5],
            pat_6=pat_flags[6],
            followup_match=followup,
            encode_request=1.0 if _ENCODE_REQUEST_PATTERN.search(text) else 0.0,
            roleplay=1.0 if _ROLEPLAY_PATTERN.search(text) else 0.0,
            multi_turn=1.0 if _MULTI_TURN_PATTERN.search(text) else 0.0,
            injection=1.0 if _INJECTION_PATTERN.search(text) else 0.0,
        )
