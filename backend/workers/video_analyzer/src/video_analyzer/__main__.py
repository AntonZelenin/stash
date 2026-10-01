"""Local entrypoint: `python -m video_analyzer`."""

from stash_worker_core.runtime import run_locally

from video_analyzer.config import get_settings
from video_analyzer.stage import SERVICE, build_worker

if __name__ == "__main__":
    run_locally(service=SERVICE, settings=get_settings(), build_worker=build_worker)
