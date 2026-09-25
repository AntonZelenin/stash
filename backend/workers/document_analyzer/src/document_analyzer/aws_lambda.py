"""Lambda entrypoint: `document_analyzer.aws_lambda.handler` (see `stash_worker_core.aws_lambda`)."""

from stash_worker_core.aws_lambda import SqsWorkerFunction

from document_analyzer.config import get_settings
from document_analyzer.stage import QUEUE, SERVICE, build_worker

handler = SqsWorkerFunction(service=SERVICE, queue_name=QUEUE, get_settings=get_settings, build_worker=build_worker)
