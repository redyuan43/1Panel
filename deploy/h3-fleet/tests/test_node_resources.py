import pytest

from app.admission import GIB, cgroup_limit_bytes


def test_64_gib_host_cannot_keep_old_72_gib_limit():
    assert cgroup_limit_bytes({"memory_total_bytes": 64 * GIB}, {"max_cgroup_gib": 72}) == 48 * GIB


def test_configured_lower_limit_and_larger_reserve_are_preserved():
    assert cgroup_limit_bytes({"memory_total_bytes": 64 * GIB}, {"max_cgroup_gib": 40}) == 40 * GIB
    assert cgroup_limit_bytes({"memory_total_bytes": 64 * GIB}, {"min_available_ram_gib": 24}) == 40 * GIB


def test_larger_host_cannot_raise_reviewed_ceiling():
    assert cgroup_limit_bytes({"memory_total_bytes": 128 * GIB}, {"max_cgroup_gib": 96}) == 72 * GIB


@pytest.mark.parametrize("total", [0, -1, 16 * GIB, True, "64"])
def test_invalid_physical_memory_fails_closed(total):
    with pytest.raises(ValueError):
        cgroup_limit_bytes({"memory_total_bytes": total}, {})
