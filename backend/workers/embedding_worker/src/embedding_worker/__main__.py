"""Local entrypoint: `python -m embedding_worker`."""

from stash_worker_core.runtime import run_locally

from embedding_worker.config import get_settings
from embedding_worker.stage import SERVICE, build_worker

if __name__ == "__main__":
    run_locally(service=SERVICE, settings=get_settings(), build_worker=build_worker)
