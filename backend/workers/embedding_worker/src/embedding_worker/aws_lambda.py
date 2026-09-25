"""Lambda entrypoint: `embedding_worker.aws_lambda.handler` (see `stash_worker_core.aws_lambda`)."""

from stash_worker_core.aws_lambda import SqsWorkerFunction

from embedding_worker.config import get_settings
from embedding_worker.stage import QUEUE, SERVICE, build_worker

handler = SqsWorkerFunction(service=SERVICE, queue_name=QUEUE, get_settings=get_settings, build_worker=build_worker)
