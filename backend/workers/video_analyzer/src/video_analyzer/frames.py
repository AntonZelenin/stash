"""Frames sampled from a video file on local disk, with the `ffmpeg`
command-line tool: first a probe (duration, size of the video stream), then
one short ffmpeg run per frame (`sampling.sample_times`), each seeking
straight to its time and writing one small JPEG to stdout.

ffmpeg runs as a child process, never in-process: a decoder crashing or
running out of memory on a hostile file kills that process, not the
worker, and every run is killed at its timeout. Its output is read with a
cap, so nothing it prints can grow unbounded in memory.

What a video may cost (`FrameLimits`), however large the upload:
- the probe: `probe_timeout_seconds`, `MAX_PROBE_OUTPUT_BYTES` of output;
- its size: `max_width` x `max_height` and `max_pixels` per frame, checked
  from the probe, and enforced again by the decoder itself (ffmpeg's
  `-max_pixels`), so a stream lying about its size can't decode bigger;
  `max_duration_seconds`, a sanity bound (the work doesn't grow with it);
- frames: at most `max_frames`, each decoded within
  `frame_timeout_seconds`, all of them within `timeout_seconds` (frames
  done by then are used), scaled down to `frame_max_dimension` and at most
  `max_frame_bytes` as JPEG; one decoder thread.

Hostile containers: ffmpeg is only ever given the demuxer of the validated
content type (`demuxer_for`), never left to guess the format from the
bytes, so a file can't make it run a playlist/concat demuxer (HLS, concat,
image sequences...) that would read other local files or URLs; and only
the `file` protocol is allowed. It gets no environment but `PATH`, so the
function's credentials aren't in its environment either."""

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from stash_worker_core.errors import MalformedInputError, ProcessingLimitExceeded

from video_analyzer.sampling import sample_times

# The validated content types the API sends here (`app.items.files`: every
# video format) -> ffmpeg's demuxer for that container.
DEMUXERS = {
    "video/mp4": "mov",
    "video/x-m4v": "mov",
    "video/quicktime": "mov",
    "video/3gpp": "mov",
    "video/webm": "matroska",
    "video/x-matroska": "matroska",
    "video/x-msvideo": "avi",
    "video/mpeg": "mpeg",
    "video/ogg": "ogg",
    "video/x-ms-wmv": "asf",
}

# What the probe may print (container info, streams, tags) before it's
# cut off: far more than any real file's header.
MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
# ffmpeg's own error output for one frame: drained, kept only this much.
_MAX_FRAME_ERROR_BYTES = 64 * 1024
# JPEG quality (2 best - 31 worst): plenty for a model to read signage.
_JPEG_QUALITY = 4
# A run that ignored a kill still gets this long to be reaped.
_KILL_GRACE_SECONDS = 5.0

# Only what ffmpeg needs to start (SYSTEMROOT: on Windows, for development).
_ENVIRONMENT = {name: value for name in ("PATH", "SYSTEMROOT") if (value := os.environ.get(name))}

# ffmpeg's report on its input. Lines are matched at their exact
# indentation (two spaces): tags (user content, names included) are
# printed indented further, and a newline in one continues at that
# indentation, so no tag can pass for one of these lines.
_INPUT = re.compile(r"^Input #0, ", re.MULTILINE)
_DURATION = re.compile(r"^  Duration: (?:(\d+):(\d{2}):(\d{2}(?:\.\d+)?)|N/A)", re.MULTILINE)
_VIDEO_STREAM = re.compile(r"^  Stream #0:\d+\S*: Video: (.*)$", re.MULTILINE)
_DIMENSIONS = re.compile(r"(?:^|, )(\d{1,6})x(\d{1,6})(?=[ ,\[]|$)")
# What ffmpeg's decoder says refusing a picture over `-max_pixels`.
_OVER_MAX_PIXELS = "exceeds specified max pixel count"


@dataclass(frozen=True)
class FrameLimits:
    """How much work one video may cost; see the module docstring."""

    max_duration_seconds: float = 12 * 3600
    max_width: int = 8192
    max_height: int = 8192
    # 8K UHD (7680 x 4320).
    max_pixels: int = 33_177_600
    max_frames: int = 20
    min_gap_seconds: float = 1.0
    frame_max_dimension: int = 768
    max_frame_bytes: int = 1024 * 1024
    probe_timeout_seconds: float = 15.0
    frame_timeout_seconds: float = 20.0
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class VideoInfo:
    # None: the container doesn't say.
    duration_seconds: float | None
    width: int
    height: int


@dataclass(frozen=True)
class Frame:
    # When it was sampled, in seconds from the start.
    time_seconds: float
    jpeg: bytes


@dataclass(frozen=True)
class SampledVideo:
    info: VideoInfo
    # In timeline order; never empty.
    frames: list[Frame]
    # How many were scheduled (`sampling.sample_times`), and why the
    # others are missing: no frame there (past the real end, undecodable),
    # the frame's run timed out, or the same picture as an earlier one.
    scheduled: int
    failed: int = 0
    timed_out: int = 0
    duplicates: int = 0
    # "time_limit" if the extraction's time ran out before every frame
    # was tried.
    stopped_by: str | None = None

    @property
    def total_jpeg_bytes(self) -> int:
        return sum(len(frame.jpeg) for frame in self.frames)


def demuxer_for(content_type: str) -> str | None:
    """ffmpeg's demuxer for a stored (validated) content type, or None if
    it isn't a video format this worker handles."""
    return DEMUXERS.get(content_type.split(";", 1)[0].strip().lower())


class FrameSampler:
    """Samples frames from a video on local disk with the `ffmpeg` binary
    at `ffmpeg_path` (a path, or a name looked up on `PATH`)."""

    def __init__(self, *, ffmpeg_path: str = "ffmpeg", limits: FrameLimits = FrameLimits()):
        self._ffmpeg = ffmpeg_path
        self._limits = limits

    async def sample(self, path: Path, *, demuxer: str) -> SampledVideo:
        """Probes the video, then extracts its frames
        (`sampling.sample_times`), in order. Frames that can't be had are
        left out; with none at all it fails, permanently: a timeout is a
        `ProcessingLimitExceeded`, anything else a `MalformedInputError`
        (ffmpeg is deterministic on the same file)."""
        limits = self._limits
        info = await self.probe(path, demuxer=demuxer)
        times = sample_times(
            info.duration_seconds, max_frames=limits.max_frames, min_gap_seconds=limits.min_gap_seconds
        )
        deadline = time.monotonic() + limits.timeout_seconds
        frames: list[Frame] = []
        seen: set[bytes] = set()
        stats = {"failed": 0, "timed_out": 0, "duplicates": 0}
        stopped_by = None
        for at in times:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped_by = "time_limit"
                break
            try:
                jpeg = await self._frame(
                    path, demuxer=demuxer, at=at, timeout=min(limits.frame_timeout_seconds, remaining)
                )
            except TimeoutError:
                stats["timed_out"] += 1
                continue
            if jpeg is None:
                stats["failed"] += 1
                continue
            # A long GOP or a still picture can give the same frame twice.
            digest = hashlib.sha256(jpeg).digest()
            if digest in seen:
                stats["duplicates"] += 1
                continue
            seen.add(digest)
            frames.append(Frame(time_seconds=at, jpeg=jpeg))
        if not frames:
            if stats["timed_out"] or stopped_by:
                raise ProcessingLimitExceeded("No video frame could be decoded within the time limit")
            raise MalformedInputError("No video frame could be decoded")
        return SampledVideo(info=info, frames=frames, scheduled=len(times), stopped_by=stopped_by, **stats)

    async def probe(self, path: Path, *, demuxer: str) -> VideoInfo:
        """The video's duration and its (first, non-cover-art) video
        stream's size, as ffmpeg reports opening it. Raises
        `MalformedInputError` if ffmpeg can't open it or it has no video,
        `ProcessingLimitExceeded` if it's over a limit or the probe is."""
        limits = self._limits
        try:
            result = await self._run(
                ["-hide_banner", "-nostdin", *self._input_options(demuxer), "-i", str(path)],
                timeout=limits.probe_timeout_seconds,
                max_stdout=0,
                max_stderr=MAX_PROBE_OUTPUT_BYTES,
            )
        except TimeoutError:
            raise ProcessingLimitExceeded("Probing the video timed out") from None
        if result.stderr_truncated:
            raise ProcessingLimitExceeded("ffmpeg's report on the video is over the limit")
        # Exits 1 even on success: a probe has no output file. Never logged
        # or put in an error: it quotes the file's tags.
        report = result.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if not _INPUT.search(report):
            raise MalformedInputError("ffmpeg could not open the video")
        video_streams = [m.group(1) for m in _VIDEO_STREAM.finditer(report) if "(attached pic)" not in m.group(1)]
        if not video_streams:
            raise MalformedInputError("The file has no video stream")
        dimensions = _DIMENSIONS.search(video_streams[0])
        if dimensions is None or 0 in (width := int(dimensions.group(1)), height := int(dimensions.group(2))):
            # Some codecs only say their size once a frame is decoded,
            # which the decoder refused to do past `max_pixels`.
            if _OVER_MAX_PIXELS in report:
                raise ProcessingLimitExceeded(f"Video frames are over the {limits.max_pixels}-pixel limit")
            raise MalformedInputError("The video stream's size is unknown")
        if width > limits.max_width or height > limits.max_height or width * height > limits.max_pixels:
            raise ProcessingLimitExceeded(f"Video is {width}x{height}, over the size limit")
        duration = _duration(report)
        if duration is not None and duration > limits.max_duration_seconds:
            raise ProcessingLimitExceeded(
                f"Video is {duration:.0f} s long, over the {limits.max_duration_seconds:.0f} s limit"
            )
        return VideoInfo(duration_seconds=duration, width=width, height=height)

    async def _frame(self, path: Path, *, demuxer: str, at: float, timeout: float) -> bytes | None:
        """One JPEG of the frame at `at` seconds (the first decodable one
        from there), scaled to fit `frame_max_dimension`; None if there's
        none (past the end, undecodable) or it's over `max_frame_bytes`.
        Raises `TimeoutError` past `timeout`."""
        size = self._limits.frame_max_dimension
        result = await self._run(
            [
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                "1",
                *self._input_options(demuxer),
                # Input seeking: straight to the keyframe before `at`, then
                # decoding only from there.
                "-ss",
                f"{at:.3f}",
                "-i",
                str(path),
                # The first real video stream (V: not cover art).
                "-map",
                "0:V:0",
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-filter_threads",
                "1",
                "-vf",
                f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-pix_fmt",
                "yuvj420p",
                "-c:v",
                "mjpeg",
                "-q:v",
                str(_JPEG_QUALITY),
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            timeout=timeout,
            max_stdout=self._limits.max_frame_bytes,
            max_stderr=_MAX_FRAME_ERROR_BYTES,
        )
        if result.returncode != 0 or result.stdout_truncated or not result.stdout.startswith(b"\xff\xd8"):
            return None
        return result.stdout

    def _input_options(self, demuxer: str) -> list[str]:
        """How every run opens the file: only as a local file, only with the
        validated container's demuxer, never decoding a picture over
        `max_pixels`."""
        return [
            "-protocol_whitelist",
            "file",
            "-max_pixels",
            str(self._limits.max_pixels),
            "-f",
            demuxer,
        ]

    async def _run(self, args: list[str], *, timeout: float, max_stdout: int, max_stderr: int) -> "_Result":
        """Runs ffmpeg with `args`, keeping at most `max_stdout`/`max_stderr`
        bytes of its output (the rest is read and dropped, so it never
        blocks on a full pipe). Killed, and `TimeoutError` raised, past
        `timeout`."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_ENVIRONMENT,
            )
        except FileNotFoundError as exc:
            # A deployment problem, not the file's: retried, then visible
            # in the dead letters.
            raise RuntimeError(f"ffmpeg not found at {self._ffmpeg!r}") from exc
        try:
            (stdout, stdout_truncated), (stderr, stderr_truncated), returncode = await asyncio.wait_for(
                asyncio.gather(_read(process.stdout, max_stdout), _read(process.stderr, max_stderr), process.wait()),
                timeout,
            )
        finally:
            if process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), _KILL_GRACE_SECONDS)
                except TimeoutError:
                    pass
        return _Result(returncode, stdout, stdout_truncated, stderr, stderr_truncated)


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: bytes
    stdout_truncated: bool
    stderr: bytes = field(repr=False)
    stderr_truncated: bool = False


async def _read(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Everything `stream` yields until EOF, keeping only the first `limit`
    bytes; and whether there was more."""
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(64 * 1024):
        if truncated:
            continue
        if len(kept) + len(chunk) > limit:
            truncated = True
            continue
        kept += chunk
    return bytes(kept), truncated


def _duration(report: str) -> float | None:
    match = _DURATION.search(report)
    if match is None or match.group(1) is None:
        return None
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    duration = hours * 3600 + minutes * 60 + seconds
    return duration if duration > 0 else None
