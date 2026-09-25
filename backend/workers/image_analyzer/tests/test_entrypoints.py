"""The Lambda handler runs this worker (the one the local entrypoint runs,
`stage.build_worker`) on a `LambdaSqsQueue`."""

from stash_worker_core.aws_lambda import SqsWorkerFunction
from stash_worker_core.worker import Worker

from image_analyzer import aws_lambda, config


def test_the_lambda_handler_runs_this_worker(lambda_db):
    assert aws_lambda.handler._get_settings is config.get_settings
    # A copy with test settings, leaving the module's handler untouched.
    handler = SqsWorkerFunction(
        service=aws_lambda.handler._service,
        queue_name=aws_lambda.handler._queue_name,
        get_settings=lambda: config.Settings(openai_api_key="sk-test"),
        build_worker=aws_lambda.handler._build_worker,
    )

    assert handler({"Records": []}, None) == {"batchItemFailures": []}
    assert (handler._queue_name, handler._service) == ("content_analysis_jobs", "image_analyzer")
    assert isinstance(handler._worker, Worker)
    assert handler._worker._queue is handler._queue
    assert handler._worker._queue_name == "content_analysis_jobs"
