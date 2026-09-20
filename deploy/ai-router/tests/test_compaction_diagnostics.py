from types import SimpleNamespace
from unittest.mock import Mock

from ai_router.api import _record_compaction_validation_failure
from ai_router.compaction import SummaryResponseError
from ai_router.route_trace import DecisionTrace


def test_compaction_validation_failure_is_recorded_without_raw_content():
    trace = DecisionTrace(
        request_id="server-request",
        client_request_id="client-request",
        client_id="workbuddy-public",
        key_id="test",
        protocol="chat",
        requested_model="siyuan/auto",
        excerpt={},
        instance_id="test",
        boot_id="test",
        settings_hash="test",
        registry_hash="test",
    )
    audit = Mock()
    current = SimpleNamespace(audit=audit)
    error = SummaryResponseError(
        200,
        "summary HTTP response failed validation",
        reason_code="invalid_json",
        diagnostics={
            "content_chars": 123,
            "content_sha256": "a" * 64,
            "json_error_position": 120,
            "content": "must never be logged",
        },
    )

    _record_compaction_validation_failure(current, trace, error)

    step = trace.payload["attempts"][0]["steps"][-1]
    assert step["node_id"] == "context_compaction"
    assert step["status"] == "error"
    assert step["reason"] == "invalid_json"
    assert step["evidence"] == {
        "reason_code": "invalid_json",
        "upstream_status_code": 200,
        "retryable": False,
        "content_chars": 123,
        "content_sha256": "a" * 64,
        "json_error_position": 120,
    }
    assert "content" not in step["evidence"]
    audit.write.assert_called_once_with(
        "compaction_validation_failed",
        request_id="server-request",
        client_request_id="client-request",
        **step["evidence"],
    )
