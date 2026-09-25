"""Local entrypoint: `python -m thumbnailer`."""

from stash_worker_core.runtime import run_locally

from thumbnailer.config import get_settings
from thumbnailer.stage import SERVICE, build_worker

if __name__ == "__main__":
    run_locally(service=SERVICE, settings=get_settings(), build_worker=build_worker)
