from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the key; replaces
    # `openai_api_key` when set.
    openai_api_key_secret_arn: str = ""
    openai_model: str = "gpt-6-sol"
    # One request with up to 20 images: longer than the image analyzer's.
    openai_timeout_seconds: float = 120.0
    # Valkey: a video can take a few minutes (download, frames, OpenAI),
    # more than the default 300 s leaves to spare.
    queue_visibility_timeout_seconds: int = 600

    # The ffmpeg binary: a path, or a name looked up on PATH. On Lambda it
    # comes from the ffmpeg layer (/opt/bin/ffmpeg).
    ffmpeg_path: str = "ffmpeg"

    # Processing limits: how much work one video may cost, however large
    # the upload (up to 500 MB). See `video_analyzer.frames` for how each
    # applies. Past one (with no frame yet), the video fails
    # (PROCESSING_LIMIT_EXCEEDED, never retried).
    # The whole file is downloaded to local disk (the function's ephemeral
    # storage, 1 GB), never into memory: ffmpeg needs random access to
    # seek. Above the upload limit, so no video is refused for its size.
    video_max_download_bytes: int = 512 * 1024 * 1024
    # A sanity bound only: the work doesn't grow with the duration (the
    # number of frames does, up to `video_max_frames`).
    video_max_duration_seconds: float = 12 * 3600
    # The largest frame decoded, checked from the probe and enforced by the
    # decoder itself: 8K. Decoding one costs ffmpeg a few hundred MB.
    video_max_width: int = 8192
    video_max_height: int = 8192
    video_max_pixels: int = 7680 * 4320
    # Most frames sampled from any video (see `video_analyzer.sampling`),
    # and the least time between two.
    video_max_frames: int = 20
    video_min_frame_gap_seconds: float = 1.0
    # Frames are scaled down to fit this square before they're sent: enough
    # for a model to read signage, a few image tiles each.
    video_frame_max_dimension: int = 768
    video_max_frame_bytes: int = 1024 * 1024
    # Wall-clock limits on ffmpeg: opening the file, each frame, and all
    # frames together (the frames done by then are described). Within the
    # function's timeout (worker_timeout_seconds, 360 s) with the download
    # before them and the OpenAI call after.
    video_probe_timeout_seconds: float = 15.0
    video_frame_timeout_seconds: float = 20.0
    video_extraction_timeout_seconds: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
