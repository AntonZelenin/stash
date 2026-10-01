"""The Lambda handler runs this worker (the one the local entrypoint runs,
`stage.build_worker`) on a `LambdaSqsQueue`."""

import sys

import pytest
from stash_worker_core.aws_lambda import SqsWorkerFunction
from stash_worker_core.worker import Worker

from video_analyzer import aws_lambda, config, stage


def test_the_lambda_handler_runs_this_worker(lambda_db):
    assert aws_lambda.handler._get_settings is config.get_settings
    # A copy with test settings, leaving the module's handler untouched.
    # Any executable stands in for ffmpeg: only its presence is checked.
    handler = SqsWorkerFunction(
        service=aws_lambda.handler._service,
        queue_name=aws_lambda.handler._queue_name,
        get_settings=lambda: config.Settings(openai_api_key="sk-test", ffmpeg_path=sys.executable),
        build_worker=aws_lambda.handler._build_worker,
    )

    assert handler({"Records": []}, None) == {"batchItemFailures": []}
    assert (handler._queue_name, handler._service) == ("video_analysis_jobs", "video_analyzer")
    assert isinstance(handler._worker, Worker)
    assert handler._worker._queue is handler._queue
    assert handler._worker._queue_name == "video_analysis_jobs"
    assert handler._worker._item_type == "file"


def test_a_worker_without_ffmpeg_does_not_start(engine):
    settings = config.Settings(openai_api_key="sk-test", ffmpeg_path="definitely-not-ffmpeg-here")

    with pytest.raises(SystemExit, match="ffmpeg not found"):
        stage.build_worker(settings, engine)


def test_limits_come_from_settings():
    settings = config.Settings(video_max_frames=7, video_frame_max_dimension=512, video_extraction_timeout_seconds=30)

    limits = stage.frame_limits(settings)

    assert (limits.max_frames, limits.frame_max_dimension, limits.timeout_seconds) == (7, 512, 30)


def test_defaults_fit_the_lambda_function():
    """The function has 1 GB of /tmp (infra lambda.tf) and a 360 s timeout
    (worker_timeout_seconds): the download fits on disk, and ffmpeg's time
    plus the OpenAI call fit in the timeout with the download before them."""
    settings = config.Settings()

    assert settings.video_max_download_bytes <= 1024**3 // 2
    assert settings.video_max_download_bytes >= 500 * 1024 * 1024  # the upload limit
    worst_case = settings.video_probe_timeout_seconds + settings.video_extraction_timeout_seconds
    assert worst_case + settings.openai_timeout_seconds <= 360 - 60
