from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_parallel_comparison as parallel


@pytest.fixture
def sampling(monkeypatch):
    def build(memory_values, proc_values, disk_used=0, backing="none"):
        memory = iter(memory_values)
        proc = iter(proc_values)
        reads = []

        def read(path):
            name = str(path)
            reads.append(name)
            if name == "/proc/meminfo":
                used = next(memory)
                return f"SwapTotal: {16 * parallel.GIB // 1024} kB\nSwapFree: {(16 * parallel.GIB - used) // 1024} kB\n"
            if name == "/proc/swaps":
                used = next(proc)
                return ("Filename Type Size Used Priority\n"
                        f"/dev/zram0 partition 16777212 {used // 1024} 100\n"
                        f"/swap.img file 8388604 {disk_used // 1024} -1\n")
            values = {"/proc/vmstat": "pswpin 100\npswpout 200\n",
                      "/sys/block/zram0/disksize": str(16 * parallel.GIB),
                      "/sys/block/zram0/backing_dev": backing,
                      "/sys/block/zram0/mm_stat": "1 1 1 0 1 0 0",
                      "/sys/block/zram0/bd_stat": "0 0 0",
                      "/sys/fs/cgroup" + parallel.CGROUP + "/memory.swap.current": str(4 * parallel.GIB)}
            return values[name]

        monkeypatch.setattr(Path, "read_text", read)
        return parallel.Host().coherent_swap_inventory(), reads
    return build


def test_real_twelve_kib_downward_race_retries_without_expanding_page_tolerance(sampling):
    before, after = 6251286528, 6251274240
    result, _ = sampling([before, after, after, after], [after, after])
    assert result["swap_sampling_consistent"]
    assert len(result["swap_sampling_attempts"]) == 2
    assert result["swap_used_bytes"] == after
    assert result["swap_sampling_peak_host_bytes"] == before
    from progressive_resource_policy import parse_inventory
    parse_inventory(dict(result, timestamp=result["swap_inventory"]["observed_at"]), result["swap_inventory"]["observed_at"])


def test_repeated_incoherence_fails_closed_after_three_attempts(sampling):
    used = 6 * parallel.GIB
    result, reads = sampling([used] * 6, [used - 12 * 1024] * 3)
    assert not result["swap_sampling_consistent"]
    assert result["swap_sampling_error"] == "host_swap_inventory_mismatch"
    assert reads.count("/proc/swaps") == 3


def test_upward_race_and_recovered_peak_are_not_lost(sampling):
    used = 6 * parallel.GIB
    higher = used + 12 * 1024
    result, _ = sampling([used, higher, used, used], [used, used])
    assert result["swap_sampling_consistent"]
    assert result["swap_used_bytes"] == used
    assert result["swap_sampling_peak_host_bytes"] == higher
    assert len(result["swap_sampling_attempts"]) == 2


def test_proc_swap_peak_above_eight_gib_cannot_disappear_on_retry(sampling):
    low = 8 * parallel.GIB - 8192
    high = 8 * parallel.GIB + 4096
    result, _ = sampling([low] * 4, [high, low])
    assert result["swap_sampling_consistent"]
    assert result["swap_sampling_peak_host_bytes"] == high
    assert result["swap_sampling_attempts"][0]["proc_used_total_bytes"] == high


def test_completed_attempt_is_retained_if_next_read_fails(sampling):
    used = 6 * parallel.GIB
    with pytest.raises(parallel.SwapSamplingError) as raised:
        sampling([used, used], [used - 12288])
    assert len(raised.value.attempts) == 2
    assert raised.value.attempts[0]["sample"]["swap_used_bytes"] == used
    assert raised.value.attempts[1]["stage"] == "reading_meminfo_before"


@pytest.mark.parametrize("disk,backing,error", [(4096, "none", "disk_swap_in_use"),
                                                (0, "8:0", "zram_backing_present_or_unknown")])
def test_real_unsafe_swap_inventory_is_never_retried_away(sampling, disk, backing, error):
    used = 6 * parallel.GIB
    result, reads = sampling([used, used], [used], disk_used=disk, backing=backing)
    assert not result["swap_sampling_consistent"]
    assert result["swap_sampling_error"] == error
    assert reads.count("/proc/swaps") == 1
