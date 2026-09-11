import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "shared/reference_video.py"
SPEC = importlib.util.spec_from_file_location("reference_video_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.fixture
def synthesize(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("CPU integration tests require ffmpeg and ffprobe")

    def create(name="clip.mp4", rate="24", duration="0.5", size="64x48", audio=False,
               extra=(), audio_rate="48000", channels="1", codec="libx264"):
        target = tmp_path / name
        command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   f"testsrc2=size={size}:rate={rate}:duration={duration}"]
        if audio:
            command.extend(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={audio_rate}:duration={duration}"])
        command.extend(["-map", "0:v:0"])
        if audio:
            command.extend(["-map", "1:a:0", "-c:a", "pcm_s16le" if target.suffix in {".mkv", ".mov"} else "aac",
                            "-ac", channels])
        command.extend(["-c:v", codec, "-threads", "1", "-pix_fmt", "yuv420p", *extra, str(target)])
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        return target

    return create


@pytest.mark.parametrize("suffix", [".mp4", ".mov", ".mkv", ".webm"])
def test_real_cfr24_and_read_only_json_contract(synthesize, suffix):
    path = synthesize(name="private-name" + suffix, codec="libvpx-vp9" if suffix == ".webm" else "libx264")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = probe.inspect_video(path)
    assert metadata["schema_version"] == 1
    assert (metadata["width"], metadata["height"], metadata["frame_count"]) == (64, 48, 12)
    assert metadata["pixel_format"] == "yuv420p"
    assert (metadata["frame_rate_numerator"], metadata["frame_rate_denominator"]) == (24, 1)
    assert metadata["is_cfr_24"] is True
    assert metadata["video_start_seconds"] == 0
    assert metadata["video_duration_seconds"] == pytest.approx(0.5, abs=0.001001)
    assert metadata["frame_timestamps_seconds"] == pytest.approx([index / 24 for index in range(12)], abs=0.001)
    assert metadata["has_audio"] is False and metadata["audio_track_count"] == 0
    assert metadata["audio_tracks"] == []
    for name in ("audio_channels", "audio_sample_rate", "audio_start_seconds", "audio_duration_seconds"):
        assert metadata[name] is None
    encoded = json.dumps(metadata, allow_nan=False)
    assert "private-name" not in encoded and str(path.parent) not in encoded
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("rate", ["30", "24000/1001"])
def test_real_non24_rates(synthesize, rate):
    metadata = probe.inspect_video(synthesize(rate=rate, duration="1"))
    assert metadata["is_cfr_24"] is False
    numerator, denominator = (int(part) for part in (rate.split("/") if "/" in rate else [rate, "1"]))
    assert metadata["frame_rate_numerator"] / metadata["frame_rate_denominator"] == pytest.approx(numerator / denominator)


def test_short_real_4k_cpu_probe_under_address_space_limit(synthesize):
    metadata = probe.inspect_video(synthesize(size="3840x2160", duration="0.25"))
    assert (metadata["width"], metadata["height"], metadata["frame_count"]) == (3840, 2160, 6)
    assert metadata["is_cfr_24"] is True


@pytest.mark.parametrize("b_frames", ["0", "2"])
def test_real_vfr_uses_display_pts_not_advertised_average(synthesize, b_frames):
    path = synthesize(duration="1", extra=(
        "-vf", "settb=1/48000,setpts=PTS+if(eq(mod(N\\,2)\\,0)*gt(N\\,0)\\,500\\,0)",
        "-vsync", "vfr", "-enc_time_base", "1/48000", "-r", "24", "-bf", b_frames,
        "-video_track_timescale", "48000"))
    advertised = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=5)
    if b_frames == "0":
        assert json.loads(advertised.stdout)["streams"][0]["avg_frame_rate"] == "24/1"
    metadata = probe.inspect_video(path)
    timestamps = metadata["frame_timestamps_seconds"]
    intervals = {round(current - previous, 6) for previous, current in zip(timestamps, timestamps[1:])}
    assert len(intervals) == 3
    assert metadata["frame_count"] == 24
    assert metadata["video_duration_seconds"] == pytest.approx(1)
    assert metadata["frame_rate_numerator"] == 24
    assert metadata["is_cfr_24"] is False


@pytest.mark.parametrize("suffix", [".mp4", ".mov", ".mkv"])
def test_real_audio_samples_and_timeline(synthesize, suffix):
    metadata = probe.inspect_video(synthesize(name="sound" + suffix, audio=True, channels="2"))
    assert metadata["has_audio"] is True and metadata["audio_track_count"] == 1
    assert metadata["audio_channels"] == 2 and metadata["audio_sample_rate"] == 48000
    assert metadata["audio_start_seconds"] == pytest.approx(0, abs=0.001)
    assert metadata["audio_duration_seconds"] == pytest.approx(0.5, abs=0.001)
    assert metadata["audio_tracks"][0]["sample_count"] >= 24000
    assert probe.decoded_memory_budget(metadata) > probe.decoded_memory_budget({
        **metadata, "has_audio": False, "audio_track_count": 0, "audio_tracks": []})


def test_real_multiple_audio_tracks_and_nonzero_start(synthesize):
    path = synthesize(name="tracks.mov", audio=True, extra=(
        "-map", "1:a:0", "-ac:a:0", "1", "-ac:a:1", "2", "-ar:a:1", "44100", "-output_ts_offset", "2"))
    metadata = probe.inspect_video(path)
    assert metadata["video_start_seconds"] == pytest.approx(2)
    assert metadata["video_duration_seconds"] == pytest.approx(0.5)
    assert metadata["audio_track_count"] == 2
    assert [track["channels"] for track in metadata["audio_tracks"]] == [1, 2]
    assert [track["sample_rate"] for track in metadata["audio_tracks"]] == [48000, 44100]
    assert metadata["audio_start_seconds"] == pytest.approx(2)
    assert metadata["audio_duration_seconds"] == pytest.approx(0.5)


def test_real_audio_offset_is_not_replaced_by_video_timeline(synthesize):
    metadata = probe.inspect_video(synthesize(name="offset.mov", audio=True,
        extra=("-af", "asetpts=PTS+0.25/TB")))
    assert metadata["video_start_seconds"] == 0
    assert metadata["audio_start_seconds"] == pytest.approx(0.25)
    assert metadata["audio_duration_seconds"] == pytest.approx(0.5)


def test_real_alpha_pixel_format_is_not_guessed_from_suffix(synthesize):
    metadata = probe.inspect_video(synthesize(name="alpha.mov", codec="qtrle",
        extra=("-pix_fmt", "argb")))
    assert metadata["pixel_format"] == "argb"


@pytest.mark.parametrize("rate,duration,reason", [("24", "16", "duration"), ("300", "14", "frame count")])
def test_real_limits_never_silently_truncate(synthesize, rate, duration, reason):
    with pytest.raises(ValueError, match=reason):
        probe.inspect_video(synthesize(size="16x16", rate=rate, duration=duration, extra=("-preset", "ultrafast")))


def test_real_duration_boundary(synthesize):
    metadata = probe.inspect_video(synthesize(size="16x16", duration="15.5", extra=("-preset", "ultrafast")))
    assert metadata["frame_count"] == 372 and metadata["video_duration_seconds"] == 15.5


def test_real_wrong_container_even_with_mp4_suffix(synthesize, tmp_path):
    path = synthesize(name="wrong.avi")
    renamed = tmp_path / "disguised.mp4"
    renamed.write_bytes(path.read_bytes())
    with pytest.raises(ValueError):
        probe.inspect_video(renamed)


def test_real_corrupt_and_truncated_video(synthesize, tmp_path):
    source = synthesize(extra=("-movflags", "+faststart"))
    content = source.read_bytes()
    for name, data in (("broken.mp4", b"not a movie"), ("truncated.mp4", content[:-200])):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(ValueError) as captured:
            probe.inspect_video(path)
        assert str(tmp_path) not in str(captured.value)


@pytest.mark.parametrize("kind", ["empty", "oversize", "directory", "missing", "fifo", "symlink"])
def test_local_file_validation_precedes_subprocess(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-input.mp4"
    if kind in {"empty", "oversize"}:
        with path.open("wb") as target:
            target.truncate(probe.MAX_BYTES + 1 if kind == "oversize" else 0)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing-target")

    def unexpected(*args, **kwargs):
        pytest.fail("invalid local files must not launch a probe")

    monkeypatch.setattr(probe.subprocess, "Popen", unexpected)
    with pytest.raises(ValueError) as captured:
        probe.inspect_video(path)
    assert "private-input" not in str(captured.value)


@pytest.mark.parametrize("replace", [False, True])
def test_source_mutation_or_replacement_invalidates_probe(tmp_path, monkeypatch, replace):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"original")

    def changed(descriptor, deadline):
        if replace:
            replacement = tmp_path / "replacement.mp4"
            replacement.write_bytes(b"new-data")
            replacement.replace(path)
        else:
            path.write_bytes(b"new-data-with-different-size")
        return {}

    monkeypatch.setattr(probe, "_inspect", changed)
    with pytest.raises(ValueError, match="source changed"):
        probe.inspect_video(path)


def fake_probe(monkeypatch, frames, stream_updates=None):
    stream = {"index": 0, "codec_type": "video", "width": 64, "height": 48, "time_base": "1/24000"}
    stream.update(stream_updates or {})

    def lines(descriptor, entries, deadline, *, frames=False):
        if frames:
            yield from frame_lines
        else:
            yield json.dumps({"streams": [stream], "format": {"format_name": "mov,mp4"}})

    frame_lines = frames
    monkeypatch.setattr(probe, "_probe_lines", lines)


def frame(timestamp, **updates):
    fields = {"media_type": "video", "stream_index": "0", "pts": str(timestamp), "pkt_duration": "1000",
              "width": "64", "height": "48", "pix_fmt": "yuv420p"}
    fields.update(updates)
    return "frame|" + "|".join(name + "=" + str(value) for name, value in fields.items() if value is not None)


@pytest.mark.parametrize("frames,updates,reason", [
    ([frame(None, pts=None, best_effort_timestamp="0")], {}, "missing frame PTS"),
    ([frame(0), frame(0)], {}, "non-increasing"),
    ([frame(1000), frame(0)], {}, "non-increasing"),
    ([frame(0), frame(1000, pkt_duration=None)], {}, "final frame duration"),
    ([frame(0), frame(1000, width="128")], {}, "changed"),
    ([frame(0), frame(1000, pix_fmt="yuv444p")], {}, "changed"),
    ([frame(0, width="4098")], {}, "dimensions"),
    ([frame(0, width="4096", height="2162")], {}, "dimensions"),
    ([frame(0), frame(1000)], {"nb_frames": "3"}, "truncation"),
    ([], {}, "empty or undecodable"),
    ([frame(0), frame(500000)], {}, "duration"),
])
def test_untrustworthy_frame_evidence_is_rejected(tmp_path, monkeypatch, frames, updates, reason):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture")
    fake_probe(monkeypatch, frames, updates)
    with pytest.raises(ValueError, match=reason):
        probe.inspect_video(path)


def test_advertised_rate_and_best_effort_do_not_override_display_pts(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture")
    fake_probe(monkeypatch, [frame(0), frame(500), frame(2000), frame(2500)],
               {"avg_frame_rate": "24/1", "r_frame_rate": "24/1"})
    assert probe.inspect_video(path)["is_cfr_24"] is False


@pytest.mark.parametrize("script,reason", [
    ("import time; time.sleep(5)", "timeout"),
    ("import os, time\nwhile True:\n os.write(1, b'x\\n'); time.sleep(0.01)", "timeout"),
    ("import os; os.write(1, b'x' * 65536)", "output exceeds"),
    ("import os; os.write(2, b'private-path-and-decode-error')", "decode error"),
    ("raise SystemExit(2)", "decode error"),
])
def test_real_child_bounds_cleanup_and_redacted_errors(tmp_path, monkeypatch, script, reason):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    real_popen = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        assert command[:5] == ["/usr/bin/prlimit", "--as=2147483648", "--cpu=90", "--core=0", "--"]
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-format_whitelist") + 1] == "mov,matroska,webm"
        assert command[command.index("-threads") + 1] == "1"
        assert "-read_intervals" not in command and "-t" not in command
        child = real_popen([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    monkeypatch.setattr(probe, "PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(probe, "MAX_OUTPUT_BYTES", 1024)
    started = time.monotonic()
    with pytest.raises(ValueError, match=reason) as captured:
        probe.inspect_video(path)
    assert time.monotonic() - started < 3
    assert "private-path" not in str(captured.value)
    assert children and all(child.poll() is not None for child in children)


def test_real_stdout_line_limit(tmp_path, monkeypatch):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    real_popen = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        child = real_popen([sys.executable, "-c", "import os; os.write(1, b'x' * 8193)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    with pytest.raises(ValueError, match="line exceeds"):
        probe.inspect_video(path)
    assert all(child.poll() is not None for child in children)


def test_missing_probe_executable_is_value_error(tmp_path, monkeypatch):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")

    def missing(*args, **kwargs):
        raise FileNotFoundError("private executable path")

    monkeypatch.setattr(probe.subprocess, "Popen", missing)
    with pytest.raises(ValueError, match="probe unavailable") as captured:
        probe.inspect_video(path)
    assert "private" not in str(captured.value)


def budget_metadata(width=64, height=48, frames=12):
    return {"schema_version": 1, "width": width, "height": height, "frame_count": frames,
            "has_audio": False, "audio_track_count": 0, "audio_tracks": []}


@pytest.mark.parametrize("width,height,frames", [(64, 48, 12), (4096, 2160, 372)])
def test_memory_budget_two_copies_plus_decoder_and_max_margin(width, height, frames):
    metadata = budget_metadata(width, height, frames)
    before = copy.deepcopy(metadata)
    frame_bytes = width * height * 4 * 4
    subtotal = 2 * frames * frame_bytes + 32 * frame_bytes
    assert probe.decoded_memory_budget(metadata) == subtotal + max(2 * 1024**3, (subtotal + 9) // 10)
    assert probe.decoded_memory_budget(metadata) > 2 * frames * width * height * 3 * 4
    assert metadata == before


@pytest.mark.parametrize("updates", [{"schema_version": 0}, {"schema_version": True}, {"frame_count": 0},
    {"width": "64"}, {"height": True}, {"frame_count": 4001}, {"has_audio": True},
    {"audio_track_count": 1}, {"width": 4096, "height": 4096}])
def test_memory_budget_rejects_incomplete_or_invalid_metadata(updates):
    with pytest.raises(ValueError):
        probe.decoded_memory_budget({**budget_metadata(), **updates})
