"""Read-only CPU inspection; decoded pictures never accumulate in Python memory."""

from __future__ import annotations

from fractions import Fraction
import json
import math
import os
import re
import selectors
import stat
import subprocess
import time


SCHEMA_VERSION = 1
MAX_BYTES = 128 * 1024**2
MAX_DIMENSION = 4096
MAX_PIXELS = 4096 * 2160
MAX_DURATION_SECONDS = 15.5
MAX_FRAMES = 4000
PROBE_TIMEOUT_SECONDS = 90
PROBE_ADDRESS_SPACE_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 8 * 1024**2
MAX_LINE_BYTES = 8192
MAX_STREAMS = 8
GIB = 1024**3


def _invalid(reason):
    return ValueError("reference_video: " + reason)


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]{1,20}", str(value)):
        raise _invalid("invalid " + name)
    result = int(value)
    if result < minimum:
        raise _invalid("invalid " + name)
    return result


def _time_base(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}/[0-9]{1,10}", value):
        raise _invalid("missing or invalid time_base")
    try:
        result = Fraction(value)
    except ZeroDivisionError:
        raise _invalid("invalid time_base") from None
    if not 0 < result <= 1:
        raise _invalid("invalid time_base")
    return result


def _dimensions(width, height):
    width = _integer(width, "width", 1)
    height = _integer(height, "height", 1)
    if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise _invalid("video dimensions exceed limit")
    return width, height


def _probe_lines(descriptor, entries, deadline, *, frames=False):
    """Drain both pipes with bounded bytes, line size and a shared wall deadline."""
    command = [
        "/usr/bin/prlimit", "--as=" + str(PROBE_ADDRESS_SPACE_BYTES), "--cpu=90", "--core=0", "--",
        "ffprobe", "-v", "error", "-threads", "1", "-max_pixels", str(MAX_PIXELS),
        "-max_alloc", str(MAX_BYTES), "-err_detect", "explode",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm",
        "-enable_drefs", "0", "-use_absolute_path", "0",
    ]
    if frames:
        command.append("-show_frames")
    command.extend(["-show_entries", entries, "-of", "compact=p=1:nk=0" if frames else "json"])
    command.extend(["-i", "/proc/self/fd/" + str(descriptor)])
    process = None
    try:
        if time.monotonic() >= deadline:
            raise _invalid("probe timeout")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, pass_fds=(descriptor,), bufsize=0)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            pending = bytearray()
            total = 0
            limit = MAX_OUTPUT_BYTES if frames else min(MAX_OUTPUT_BYTES, 256 * 1024)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _invalid("probe timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise _invalid("probe output exceeds limit")
                    if key.fileobj is process.stderr:
                        raise _invalid("invalid media or decode error")
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        if len(line) > MAX_LINE_BYTES:
                            raise _invalid("probe line exceeds limit")
                        if time.monotonic() >= deadline:
                            raise _invalid("probe timeout")
                        yield line.decode("utf-8")
                    if len(pending) > MAX_LINE_BYTES:
                        raise _invalid("probe line exceeds limit")
            if pending:
                yield pending.decode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _invalid("probe timeout")
            if process.wait(timeout=remaining) != 0:
                raise _invalid("invalid media or decode error")
    except subprocess.TimeoutExpired:
        raise _invalid("probe timeout") from None
    except (OSError, UnicodeError):
        raise _invalid("probe unavailable or invalid output") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


class _Track:
    def __init__(self, stream):
        self.index = _integer(stream.get("index"), "stream index")
        self.kind = stream.get("codec_type")
        self.time_base = _time_base(stream.get("time_base"))
        self.count = 0
        self.first = None
        self.last = None
        self.end = None
        self.last_duration = None
        self.timestamps = []
        self.samples = 0
        if self.kind == "video":
            _dimensions(stream.get("width"), stream.get("height"))
            self.shape = None
            self.declared_count = stream.get("nb_frames")
            self.stream_start = stream.get("start_pts")
            self.stream_duration = stream.get("duration_ts")
        elif self.kind == "audio":
            self.channels = _integer(stream.get("channels"), "audio channels", 1)
            self.sample_rate = _integer(stream.get("sample_rate"), "audio sample_rate", 1)
            if self.channels > 64 or self.sample_rate > 384000:
                raise _invalid("audio parameters exceed limit")
        else:
            raise _invalid("unsupported stream type")

    def accept(self, frame):
        if frame.get("media_type") != self.kind:
            raise _invalid("inconsistent frame type")
        raw_pts = frame.get("pts", frame.get("pkt_pts"))
        if raw_pts in (None, "N/A"):
            raise _invalid("missing frame PTS")
        timestamp = _integer(raw_pts, "frame PTS", -(2**63)) * self.time_base
        if self.last is not None and timestamp <= self.last:
            raise _invalid("non-increasing frame PTS")
        raw_duration = frame.get("duration", frame.get("pkt_duration"))
        duration = None
        if raw_duration not in (None, "N/A", "0"):
            duration = _integer(raw_duration, "frame duration", 1) * self.time_base
        if self.kind == "video":
            width, height = _dimensions(frame.get("width"), frame.get("height"))
            pixel_format = frame.get("pix_fmt", "")
            if not re.fullmatch(r"[a-z0-9_]{1,40}", pixel_format) or pixel_format == "unknown":
                raise _invalid("invalid pixel format")
            shape = (width, height, pixel_format)
            if self.shape is not None and shape != self.shape:
                raise _invalid("video dimensions or pixel format changed")
            self.shape = shape
            if self.count >= MAX_FRAMES:
                raise _invalid("video frame count exceeds limit")
            self.timestamps.append(timestamp)
        else:
            if _integer(frame.get("channels"), "frame channels", 1) != self.channels:
                raise _invalid("audio channels changed")
            if "sample_rate" in frame and _integer(frame["sample_rate"], "frame sample_rate", 1) != self.sample_rate:
                raise _invalid("audio sample_rate changed")
            samples = _integer(frame.get("nb_samples"), "audio samples", 1)
            sample_duration = Fraction(samples, self.sample_rate)
            if duration is not None and duration > sample_duration + self.time_base:
                raise _invalid("inconsistent audio frame duration")
            duration = min(sample_duration, duration) if duration is not None else sample_duration
            self.samples += samples
            if self.samples > math.ceil(MAX_DURATION_SECONDS * self.sample_rate) + 8192:
                raise _invalid("audio sample count exceeds limit")
        if self.first is None:
            self.first = timestamp
        self.last = timestamp
        self.last_duration = duration
        frame_end = timestamp + (duration or 0)
        self.end = max(self.end, frame_end) if self.end is not None else frame_end
        self.count += 1
        if self.end - self.first > MAX_DURATION_SECONDS:
            raise _invalid("actual duration exceeds limit")

    def finish(self):
        if not self.count:
            raise _invalid("empty or undecodable stream")
        if self.last_duration is None:
            raise _invalid("missing final frame duration")
        if self.kind == "video":
            self.end = self.last + self.last_duration
            if self.declared_count not in (None, "N/A", "0"):
                if _integer(self.declared_count, "declared frame count", 1) != self.count:
                    raise _invalid("decoded frame count mismatch; possible truncation")
                if self.stream_start not in (None, "N/A") and self.stream_duration not in (None, "N/A"):
                    stream_start = _integer(self.stream_start, "stream start PTS", -(2**63)) * self.time_base
                    stream_end = stream_start + _integer(self.stream_duration, "stream duration", 1) * self.time_base
                    if abs(stream_start - self.first) > self.time_base or stream_end <= self.last:
                        raise _invalid("inconsistent video presentation timeline")
                    self.end = max(self.end, stream_end)
                    self.last_duration = self.end - self.last
        if self.end - self.first > MAX_DURATION_SECONDS:
            raise _invalid("actual duration exceeds limit")
        return self.end - self.first


def _inspect(descriptor, deadline):
    entries = ("stream=index,codec_type,width,height,time_base,start_pts,duration_ts,nb_frames,sample_rate,channels:"
               "stream_disposition=attached_pic:stream_tags=:side_data=:format=format_name")
    try:
        header = json.loads("\n".join(_probe_lines(descriptor, entries, deadline)))
    except (json.JSONDecodeError, TypeError):
        raise _invalid("invalid probe header") from None
    streams = header.get("streams", [])
    formats = set(header.get("format", {}).get("format_name", "").split(","))
    if not formats.intersection({"mov", "matroska", "webm"}):
        raise _invalid("unsupported video container")
    if not streams or len(streams) > MAX_STREAMS:
        raise _invalid("stream count exceeds limit or no streams")
    if any(stream.get("disposition", {}).get("attached_pic") for stream in streams):
        raise _invalid("attached pictures are not reference video")
    tracks = [_Track(stream) for stream in streams]
    by_index = {track.index: track for track in tracks}
    videos = [track for track in tracks if track.kind == "video"]
    audios = [track for track in tracks if track.kind == "audio"]
    if len(videos) != 1 or len(by_index) != len(tracks):
        raise _invalid("exactly one video stream is required")
    entries = ("frame=media_type,stream_index,pts,pkt_pts,duration,pkt_duration,width,height,"
               "pix_fmt,nb_samples,channels,sample_rate:side_data=:frame_tags=")
    lines = _probe_lines(descriptor, entries, deadline, frames=True)
    try:
        for line in lines:
            line = line.partition("side_data|")[0].rstrip("|")
            if not line:
                continue
            fields = line.split("|")
            if fields.pop(0) != "frame" or any("=" not in field for field in fields):
                raise _invalid("invalid frame output")
            frame = dict(field.split("=", 1) for field in fields)
            track = by_index.get(_integer(frame.get("stream_index"), "frame stream index"))
            if track is None:
                raise _invalid("unknown frame stream")
            track.accept(frame)
    finally:
        lines.close()
    for track in tracks:
        track.finish()
    if max(track.end for track in tracks) - min(track.first for track in tracks) > MAX_DURATION_SECONDS:
        raise _invalid("actual audio/video timeline exceeds duration limit")
    video = videos[0]
    duration = video.end - video.first
    tolerance = video.time_base
    is_cfr_24 = (
        video.count > 1 and tolerance <= Fraction(1, 1000)
        and all(abs(timestamp - video.first - Fraction(index, 24)) <= tolerance
                for index, timestamp in enumerate(video.timestamps))
        and all(abs(current - previous - Fraction(1, 24)) <= tolerance
                for previous, current in zip(video.timestamps, video.timestamps[1:]))
        and abs(video.last_duration - Fraction(1, 24)) <= tolerance
    )
    rate = Fraction(24) if is_cfr_24 else (
        Fraction(video.count - 1, 1) / (video.last - video.first) if video.count > 1 else 1 / duration
    )
    audio_tracks = [{"stream_index": track.index, "channels": track.channels,
                     "sample_rate": track.sample_rate, "sample_count": track.samples,
                     "start_seconds": float(track.first), "duration_seconds": float(track.end - track.first)}
                    for track in audios]
    primary_audio = audio_tracks[0] if audio_tracks else {}
    width, height, pixel_format = video.shape
    return {
        "schema_version": SCHEMA_VERSION, "width": width, "height": height,
        "frame_count": video.count, "pixel_format": pixel_format,
        "frame_rate_numerator": rate.numerator, "frame_rate_denominator": rate.denominator,
        "is_cfr_24": is_cfr_24, "video_start_seconds": float(video.first),
        "video_duration_seconds": float(duration),
        "frame_timestamps_seconds": [float(timestamp) for timestamp in video.timestamps],
        "has_audio": bool(audios), "audio_track_count": len(audios), "audio_tracks": audio_tracks,
        "audio_channels": primary_audio.get("channels"),
        "audio_sample_rate": primary_audio.get("sample_rate"),
        "audio_start_seconds": primary_audio.get("start_seconds"),
        "audio_duration_seconds": primary_audio.get("duration_seconds"),
    }


def inspect_video(path):
    """Return JSON-safe schema v1 from complete, sequential software decoding.

    PTS are presentation timestamps, not packet DTS or synthesized best-effort
    timestamps. Display intervals come from successive PTS and the final frame
    duration. When the decoded count matches the container sample count, its
    terminal presentation time also bounds the end: reordered packets can carry
    shorter durations than their display interval. This never shortens decoded
    evidence. CFR24 requires a 24 Hz presentation grid within one time-base tick
    (at most 1 ms); advertised average/rate metadata is never used. Other rates
    describe the observed PTS span, not a promise of constant frame rate.

    Scalar audio fields describe the first audio track; audio_tracks describes
    every track, including decoded sample counts for memory accounting. Absent
    audio has null scalar fields. Extra streams and changing frame formats are
    rejected rather than silently dropped. No paths or source tags are returned.
    """
    descriptor = None
    deadline = time.monotonic() + min(PROBE_TIMEOUT_SECONDS, 90)
    try:
        source_path = os.fspath(path)
        descriptor = os.open(source_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise _invalid("expected a non-empty regular file <= 128 MiB")
        result = _inspect(descriptor, deadline)
        after = os.fstat(descriptor)
        current = os.stat(source_path, follow_symlinks=False)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise _invalid("source changed during inspection")
        if time.monotonic() >= deadline:
            raise _invalid("probe timeout")
        return result
    except (OSError, TypeError):
        raise _invalid("unable to read local video file") from None
    except ValueError as error:
        if str(error).startswith("reference_video: "):
            raise
        raise _invalid("invalid video data or path") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def decoded_memory_budget(metadata):
    """Return conservative admission bytes, NOT a measured or guaranteed peak.

    Only pass an inspect_video result from trusted storage, never caller claims;
    schema_version is a format marker, not authentication. F is a whole video
    in float32 RGBA (also covers RGB). 2F is only the two-copy lower bound. Add
    32 float32 RGBA frame buffers for decoding/conversion, two float32 audio
    copies across all tracks, then max(2 GiB, ceil(10% of the subtotal)). Runtime
    allocations and concurrency still require independent admission policy.
    """
    if not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int or metadata["schema_version"] != SCHEMA_VERSION:
        raise _invalid("trusted schema_version=1 metadata required")
    for name in ("width", "height", "frame_count", "audio_track_count"):
        if type(metadata.get(name)) is not int:
            raise _invalid("invalid budget " + name)
    width, height = _dimensions(metadata["width"], metadata["height"])
    frames = _integer(metadata["frame_count"], "frame_count", 1)
    if frames > MAX_FRAMES:
        raise _invalid("video frame count exceeds limit")
    audio_tracks = metadata.get("audio_tracks")
    if (not isinstance(audio_tracks, list) or len(audio_tracks) >= MAX_STREAMS
            or metadata["audio_track_count"] != len(audio_tracks)
            or type(metadata.get("has_audio")) is not bool or metadata["has_audio"] != bool(audio_tracks)):
        raise _invalid("invalid budget audio tracks")
    audio_bytes = 0
    for track in audio_tracks:
        if not isinstance(track, dict) or any(type(track.get(name)) is not int for name in ("sample_count", "channels", "sample_rate")):
            raise _invalid("invalid budget audio parameters")
        samples = _integer(track["sample_count"], "audio sample_count", 1)
        channels = _integer(track["channels"], "audio channels", 1)
        sample_rate = _integer(track["sample_rate"], "audio sample_rate", 1)
        if channels > 64 or sample_rate > 384000 or samples > math.ceil(MAX_DURATION_SECONDS * sample_rate) + 8192:
            raise _invalid("budget audio parameters exceed limit")
        audio_bytes += 2 * samples * channels * 4
    float_rgba_frame = width * height * 4 * 4
    subtotal = 2 * frames * float_rgba_frame + 32 * float_rgba_frame + audio_bytes
    return subtotal + max(2 * GIB, (subtotal + 9) // 10)
