from functools import lru_cache

from stash_worker_core.config import WorkerSettings


class Settings(WorkerSettings):
    # Thumbnails fit in this many pixels on their longest side: enough for
    # the grid on high-DPI screens and for OpenAI to read text in
    # screenshots, while typically tens to low hundreds of KB as WebP.
    thumbnail_max_size: int = 1024
    thumbnail_quality: int = 80
    # Processing limits (see `thumbnailer.handler.make_thumbnail`), checked
    # from the image's header before anything is decoded. Over one, the
    # image fails (PROCESSING_LIMIT_EXCEEDED), permanently.
    # Either side, as stored.
    thumbnail_max_width: int = 50_000
    thumbnail_max_height: int = 50_000
    # Pixels as stored (also Pillow's own decompression-bomb limit). Room
    # for 200 MP phone photos, which JPEG decodes at a fraction of that.
    thumbnail_max_declared_pixels: int = 250_000_000
    # Most pixels decoded, after a JPEG's reduced-scale decoding: bounds
    # the decoded image to 200 MB (4 bytes a pixel), which with one more
    # copy for palette images fits the function's 1 GB. 50 MP is far beyond
    # any screenshot or non-JPEG photo.
    thumbnail_max_pixels: int = 50_000_000
    # Largest original read (to local disk, not memory): the API's image
    # upload limit (MAX_IMAGE_UPLOAD_BYTES).
    thumbnail_max_source_bytes: int = 100 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
