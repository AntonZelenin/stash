"""Frame sampling with the real ffmpeg (on PATH, or FFMPEG_PATH), on videos
it makes itself with its built-in encoders. Skipped without ffmpeg."""

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from stash_worker_core.errors import MalformedInputError, ProcessingLimitExceeded

from video_analyzer.frames import DEMUXERS, FrameLimits, FrameSampler, demuxer_for

FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed (set FFMPEG_PATH)")


def _make(path: Path, *args: str) -> Path:
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *args, str(path)], check=True, timeout=60)
    return path


@pytest.fixture(scope="module")
def videos(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("videos")
    moving = "testsrc2=size=1280x720:rate=10:duration={}"
    return {
        # 12 s of a moving test pattern, keyframes 5 s apart.
        "mp4": _make(directory / "a", "-f", "lavfi", "-i", moving.format(12), "-c:v", "mpeg4", "-g", "50", "-f", "mp4"),
        "mkv": _make(directory / "b", "-f", "lavfi", "-i", moving.format(3), "-c:v", "mpeg4", "-f", "matroska"),
        "avi": _make(directory / "c", "-f", "lavfi", "-i", moving.format(40), "-c:v", "mpeg4", "-f", "avi"),
        # Phone-style portrait.
        "portrait": _make(
            directory / "d",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1280:rate=10:duration=4",
            "-c:v",
            "mpeg4",
            "-f",
            "mp4",
        ),
        # The same picture throughout.
        "still": _make(
            directory / "e",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=320x240:rate=5:duration=20",
            "-c:v",
            "mpeg4",
            "-f",
            "mp4",
        ),
        # Sound only, with a tag pretending to be a video stream.
        "audio": _make(
            directory / "f",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=3",
            "-metadata",
            "title=x\n  Stream #0:5: Video: h264, 64x64",
            "-c:a",
            "aac",
            "-f",
            "mp4",
        ),
        # Sound with cover art: a video stream, but not a video.
        "cover": _make(
            directory / "g",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=64x64:duration=1",
            "-map",
            "0",
            "-map",
            "1",
            "-frames:v",
            "1",
            "-c:a",
            "aac",
            "-c:v",
            "mjpeg",
            "-disposition:v",
            "attached_pic",
            "-f",
            "mp4",
        ),
    }


def _sampler(**limits) -> FrameSampler:
    return FrameSampler(ffmpeg_path=FFMPEG, limits=FrameLimits(**limits))


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a baseline JPEG's SOF0 header."""
    at = 2
    while at < len(data):
        marker, length = data[at + 1], struct.unpack(">H", data[at + 2 : at + 4])[0]
        if marker == 0xC0:
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height
        at += 2 + length
    raise AssertionError("no SOF0 marker")


async def test_probe_reports_duration_and_size(videos):
    info = await _sampler().probe(videos["mp4"], demuxer="mov")

    assert info.duration_seconds == pytest.approx(12, abs=0.2)
    assert (info.width, info.height) == (1280, 720)


async def test_samples_frames_across_the_whole_video_scaled_down(videos):
    sampled = await _sampler().sample(videos["mp4"], demuxer="mov")

    # Up to 30 s: 5 frames, the middle of each 2.4 s segment.
    assert sampled.scheduled == 5
    assert [frame.time_seconds for frame in sampled.frames] == pytest.approx([1.2, 3.6, 6.0, 8.4, 10.8])
    for frame in sampled.frames:
        assert frame.jpeg.startswith(b"\xff\xd8")
        assert _jpeg_size(frame.jpeg) == (768, 432)
    assert (sampled.failed, sampled.timed_out, sampled.duplicates, sampled.stopped_by) == (0, 0, 0, None)


async def test_portrait_frames_fit_the_same_square(videos):
    sampled = await _sampler().sample(videos["portrait"], demuxer="mov")

    assert {_jpeg_size(frame.jpeg) for frame in sampled.frames} == {(432, 768)}


async def test_frames_are_never_upscaled(videos):
    sampled = await _sampler(frame_max_dimension=2000).sample(videos["mkv"], demuxer="matroska")

    assert {_jpeg_size(frame.jpeg) for frame in sampled.frames} == {(1280, 720)}


async def test_a_short_clip_gets_frames_at_least_a_second_apart(videos):
    sampled = await _sampler().sample(videos["mkv"], demuxer="matroska")

    assert [frame.time_seconds for frame in sampled.frames] == pytest.approx([0.5, 1.5, 2.5])


async def test_other_containers(videos):
    sampled = await _sampler().sample(videos["avi"], demuxer="avi")

    # 30 s - 2 min: 8 frames.
    assert len(sampled.frames) == 8


async def test_the_same_picture_is_sent_once(videos):
    sampled = await _sampler().sample(videos["still"], demuxer="mov")

    assert len(sampled.frames) == 1
    assert sampled.duplicates == sampled.scheduled - 1


async def test_sound_only_is_not_a_video_whatever_its_tags_say(videos):
    with pytest.raises(MalformedInputError, match="no video stream"):
        await _sampler().sample(videos["audio"], demuxer="mov")


async def test_cover_art_is_not_a_video(videos):
    with pytest.raises(MalformedInputError, match="no video stream"):
        await _sampler().sample(videos["cover"], demuxer="mov")


async def test_garbage_is_malformed(tmp_path):
    path = tmp_path / "video"
    path.write_bytes(b"\x00\x00\x00\x20ftypisom" + os.urandom(4096))

    with pytest.raises(MalformedInputError):
        await _sampler().sample(path, demuxer="mov")


async def test_only_the_validated_container_is_ever_opened(tmp_path, videos):
    """A playlist (or any other format) passed off as an MP4 is never run
    as one: ffmpeg is only given the validated type's demuxer, so it can't
    be made to read other files or URLs."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    playlist = tmp_path / "video"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:{secret.as_posix()}\n#EXT-X-ENDLIST\n")

    with pytest.raises(MalformedInputError, match="could not open"):
        await _sampler().sample(playlist, demuxer="mov")
    # Nor a real video in another container than the one validated.
    with pytest.raises(MalformedInputError):
        await _sampler().sample(videos["mkv"], demuxer="mov")


async def test_videos_over_the_size_limit_fail_before_decoding(videos):
    with pytest.raises(ProcessingLimitExceeded, match="limit"):
        await _sampler(max_pixels=1280 * 720 - 1).sample(videos["mp4"], demuxer="mov")
    with pytest.raises(ProcessingLimitExceeded):
        await _sampler(max_width=1000).sample(videos["mp4"], demuxer="mov")


async def test_the_decoder_enforces_the_pixel_limit_too(videos, monkeypatch):
    """Even if the probe were fooled, ffmpeg itself refuses to decode a
    picture over `max_pixels`: no frame."""
    sampler = _sampler(max_pixels=1000)
    monkeypatch.setattr(sampler, "probe", _fake_probe)

    with pytest.raises(MalformedInputError, match="No video frame"):
        await sampler.sample(videos["mp4"], demuxer="mov")


async def _fake_probe(path, *, demuxer):
    from video_analyzer.frames import VideoInfo

    return VideoInfo(duration_seconds=12, width=10, height=10)


async def test_videos_over_the_duration_limit_fail(videos):
    with pytest.raises(ProcessingLimitExceeded, match="12 s long"):
        await _sampler(max_duration_seconds=10).sample(videos["mp4"], demuxer="mov")


async def test_frames_past_the_time_limit_are_not_waited_for(videos):
    with pytest.raises(ProcessingLimitExceeded, match="time limit"):
        await _sampler(frame_timeout_seconds=0.001).sample(videos["mp4"], demuxer="mov")


async def test_frames_over_the_byte_limit_are_left_out(videos):
    with pytest.raises(MalformedInputError, match="No video frame"):
        await _sampler(max_frame_bytes=100).sample(videos["mp4"], demuxer="mov")


async def test_missing_ffmpeg_is_not_the_files_fault(videos):
    sampler = FrameSampler(ffmpeg_path=str(Path(videos["mp4"]).parent / "no-ffmpeg-here"))

    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        await sampler.sample(videos["mp4"], demuxer="mov")


def test_demuxers_by_content_type():
    assert demuxer_for("video/mp4") == "mov"
    assert demuxer_for("Video/WebM; codecs=vp9") == "matroska"
    assert demuxer_for("application/pdf") is None
    assert demuxer_for("audio/mp4") is None


def test_every_video_type_the_api_analyzes_has_a_demuxer():
    """Must match the video formats `app.items.files` on the API side
    marks for video analysis (and so enqueues here): a missing demuxer
    would fail every such upload."""
    api_video_types = {
        "video/mp4", "video/x-m4v", "video/quicktime", "video/3gpp", "video/webm", "video/x-matroska",
        "video/x-msvideo", "video/mpeg", "video/ogg", "video/x-ms-wmv",
    }  # fmt: skip

    assert api_video_types == set(DEMUXERS)
