"""Tests for A/B agreement detection and the review-queue query.

Run inside the container:
    docker exec <CID> python3 -m pytest tests/test_privacy_agreement.py -q
"""
from __future__ import annotations

import pytest

from ai_router.privacy_review import PrivacyReviewer


@pytest.mark.parametrize(
    ("lr", "decision", "valid", "expected"),
    [
        # Disagreement: LLM flags disclosure, LR let it through -> B missed it
        (False, "internal_info", True, "leak_suspect"),
        # Disagreement: LR blocked, LLM saw nothing -> possible false alarm
        (True, "normal", True, "false_alarm"),
        # Agreement, both positive
        (True, "internal_info", True, "agree_positive"),
        # Agreement, both negative
        (False, "normal", True, "agree_negative"),
        # LLM abstained -> no comparison
        (False, "uncertain", True, None),
        (True, "uncertain", True, None),
        # LLM failed -> no comparison
        (False, "uncertain", False, None),
        (True, "normal", False, None),
        # LR verdict unavailable (identity disabled) -> no comparison
        (None, "internal_info", True, None),
        (None, "normal", True, None),
    ],
)
def test_agreement_matrix(lr, decision, valid, expected):
    result = PrivacyReviewer._with_agreement(
        {"lr_decision": lr},
        {"decision": decision, "valid": valid},
    )
    if expected is None:
        assert "agreement" not in result
    else:
        assert result["agreement"] == expected


def test_agreement_preserves_original_result_fields():
    result = PrivacyReviewer._with_agreement(
        {"lr_decision": False},
        {"decision": "internal_info", "reason": "internal_identity",
         "valid": True, "elapsed_ms": 1461},
    )
    assert result["decision"] == "internal_info"
    assert result["reason"] == "internal_identity"
    assert result["elapsed_ms"] == 1461
    assert result["agreement"] == "leak_suspect"
