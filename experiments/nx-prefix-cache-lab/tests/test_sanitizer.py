from prefix_cache_lab.sanitizer import likely_contains_secret, sanitize_text


def test_sanitizer_redacts_repeatably() -> None:
    value = (
        "Authorization: Bearer abcdefghijklmnop\n"
        '"api_key":"secret-value-123"\n'
        "owner@example.com C:\\Users\\Ivan\\project"
    )
    first, counts = sanitize_text(value)
    second, _ = sanitize_text(value)
    assert first == second
    assert "abcdefghijklmnop" not in first
    assert "secret-value-123" not in first
    assert "owner@example.com" not in first
    assert "C:\\Users\\Ivan" not in first
    assert counts == {"authorization": 1, "secret-field": 1, "email": 1}
    assert not likely_contains_secret(first)


def test_secret_detector_rejects_unredacted_authorization() -> None:
    assert likely_contains_secret("Authorization: Bearer still-a-secret-value")
