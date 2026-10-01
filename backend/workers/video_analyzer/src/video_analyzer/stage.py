import shutil

from sqlalchemy.ext.asyncio import AsyncEngine
from stash_shared.queue.base import EMBEDDING_JOBS, VIDEO_ANALYSIS_JOBS, ItemType, JobQueue
from stash_worker_core.runtime import build_object_store, build_outbox, build_stage_worker, require_openai_key
from stash_worker_core.worker import Worker

from video_analyzer.config import Settings
from video_analyzer.describer import OpenAIVideoDescriber
from video_analyzer.frames import FrameLimits, FrameSampler
from video_analyzer.handler import VideoAnalysisHandler

SERVICE = "video_analyzer"
QUEUE = VIDEO_ANALYSIS_JOBS


def build_worker(settings: Settings, engine: AsyncEngine, *, queue: JobQueue | None = None) -> Worker:
    """Consumes `VIDEO_ANALYSIS_JOBS`: samples frames from each video and
    describes them via OpenAI."""
    require_openai_key(settings.openai_api_key)
    require_ffmpeg(settings.ffmpeg_path)
    return build_stage_worker(
        settings,
        queue_name=QUEUE,
        engine=engine,
        queue=queue,
        item_type=ItemType.file,
        handler=VideoAnalysisHandler(
            storage=build_object_store(settings),
            sampler=FrameSampler(ffmpeg_path=settings.ffmpeg_path, limits=frame_limits(settings)),
            describer=OpenAIVideoDescriber(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
            ),
            engine=engine,
            outbox=build_outbox(settings, engine, publishes=[EMBEDDING_JOBS]),
            max_download_bytes=settings.video_max_download_bytes,
        ),
    )


def frame_limits(settings: Settings) -> FrameLimits:
    return FrameLimits(
        max_duration_seconds=settings.video_max_duration_seconds,
        max_width=settings.video_max_width,
        max_height=settings.video_max_height,
        max_pixels=settings.video_max_pixels,
        max_frames=settings.video_max_frames,
        min_gap_seconds=settings.video_min_frame_gap_seconds,
        frame_max_dimension=settings.video_frame_max_dimension,
        max_frame_bytes=settings.video_max_frame_bytes,
        probe_timeout_seconds=settings.video_probe_timeout_seconds,
        frame_timeout_seconds=settings.video_frame_timeout_seconds,
        timeout_seconds=settings.video_extraction_timeout_seconds,
    )


def require_ffmpeg(ffmpeg_path: str) -> None:
    """Fails at startup, not on every job, if there's no ffmpeg to run."""
    if shutil.which(ffmpeg_path) is None:
        raise SystemExit(f"ffmpeg not found (FFMPEG_PATH={ffmpeg_path!r})")
