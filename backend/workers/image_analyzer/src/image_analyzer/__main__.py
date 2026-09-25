"""Local entrypoint: `python -m image_analyzer`."""

from stash_worker_core.runtime import run_locally

from image_analyzer.config import get_settings
from image_analyzer.stage import SERVICE, build_worker

if __name__ == "__main__":
    run_locally(service=SERVICE, settings=get_settings(), build_worker=build_worker)
