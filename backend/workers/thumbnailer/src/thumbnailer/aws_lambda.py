"""Lambda entrypoint: `thumbnailer.aws_lambda.handler` (see `stash_worker_core.aws_lambda`)."""

from stash_worker_core.aws_lambda import SqsWorkerFunction

from thumbnailer.config import get_settings
from thumbnailer.stage import QUEUE, SERVICE, build_worker

handler = SqsWorkerFunction(service=SERVICE, queue_name=QUEUE, get_settings=get_settings, build_worker=build_worker)
