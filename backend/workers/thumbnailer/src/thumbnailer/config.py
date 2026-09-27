from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    # Thumbnails fit in this many pixels on their longest side: enough for
    # the grid on high-DPI screens and for OpenAI to read text in
    # screenshots, while typically tens to low hundreds of KB as WebP.
    thumbnail_max_size: int = 1024
    thumbnail_quality: int = 80
    # Most pixels decoded per image, after a JPEG's reduced-scale decoding
    # (see `thumbnailer.handler.make_thumbnail`): bounds memory to what the
    # function's 1 GB holds (~4 bytes a pixel, a few copies). Over it, the
    # image fails; 50 MP is far beyond any screenshot or non-JPEG photo.
    thumbnail_max_pixels: int = 50_000_000
    # Largest original read: the API's image upload limit
    # (MAX_IMAGE_UPLOAD_BYTES).
    thumbnail_max_source_bytes: int = 100 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
