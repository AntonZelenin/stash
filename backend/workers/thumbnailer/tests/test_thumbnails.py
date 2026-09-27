import io
import tempfile
import uuid
import warnings

import pytest
from PIL import Image
from sqlalchemy import text
from stash_shared import storage_keys
from stash_shared.queue.base import CONTENT_ANALYSIS_JOBS, Delivery, ImageRef, ItemType, ProcessingJob
from stash_worker_core.errors import MalformedInputError, PermanentProcessingError, ProcessingLimitExceeded
from stash_worker_core.storage import DOWNLOAD_CHUNK_BYTES
from stash_worker_core.testing import (
    OWNER_ID,
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_status,
    fetch_thumbnail_key,
    insert_item,
    outbox_for,
)
from stash_worker_core.worker import Worker

from thumbnailer.handler import ImageLimits, ThumbnailHandler, make_thumbnail

_CORRUPT_PNG = bytes.fromhex("89504e470d0a1a0a") + b" corrupt"
_ORIGINAL_KEY = f"users/{OWNER_ID}/images/original.png"


def thumbnail_key(item_id: uuid.UUID) -> str:
    return storage_keys.thumbnail_key(OWNER_ID, item_id)


def _encode(image: Image.Image, format: str = "PNG", **params) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format, **params)
    return output.getvalue()


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# ---- make_thumbnail ----


def test_downscales_to_fit_keeping_aspect_ratio():
    original = _encode(Image.new("RGB", (4000, 2000), "orange"))

    thumbnail = _decode(make_thumbnail(original, max_size=1024, quality=80))

    assert thumbnail.format == "WEBP"
    assert thumbnail.size == (1024, 512)


def test_never_upscales_small_images():
    thumbnail = _decode(make_thumbnail(_encode(Image.new("RGB", (300, 200))), max_size=1024, quality=80))

    assert thumbnail.size == (300, 200)


def test_applies_exif_orientation():
    """A landscape-stored photo tagged "rotate 90°" must come out portrait,
    as the phone that took it shows it."""
    image = Image.new("RGB", (400, 200))
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise
    original = _encode(image, "JPEG", exif=exif)

    thumbnail = _decode(make_thumbnail(original, max_size=1024, quality=80))

    assert thumbnail.size == (200, 400)


def test_keeps_transparency():
    original = _encode(Image.new("RGBA", (50, 50), (255, 0, 0, 0)))

    thumbnail = _decode(make_thumbnail(original, max_size=1024, quality=80))

    assert thumbnail.mode == "RGBA"
    assert thumbnail.getpixel((0, 0))[3] == 0


def test_large_jpeg_is_decoded_at_reduced_scale():
    """A photo past Pillow's default bomb guard (89 MP) still gets a
    thumbnail, decoded at 1/8 scale."""
    original = _encode(Image.new("RGB", (12_000, 9_000), "navy"), "JPEG")

    thumbnail = _decode(make_thumbnail(original, max_size=1024, quality=80, limits=ImageLimits(max_pixels=5_000_000)))

    assert thumbnail.size == (1024, 768)


def test_decoding_over_the_pixel_limit_is_refused_before_decoding():
    # A tiny file declaring 20000x20000 pixels (a decompression bomb).
    original = _encode(Image.new("1", (20_000, 20_000)), "PNG")
    assert len(original) < 100_000

    with pytest.raises(PermanentProcessingError, match="pixel limit"):
        make_thumbnail(original, max_size=1024, quality=80, limits=ImageLimits(max_pixels=50_000_000))


def test_jpeg_too_large_even_at_reduced_scale_is_refused():
    original = _encode(Image.new("L", (16_000, 16_000)), "JPEG")

    with pytest.raises(PermanentProcessingError, match="pixel limit"):
        make_thumbnail(original, max_size=1024, quality=80, limits=ImageLimits(max_pixels=1_000_000))


@pytest.mark.parametrize(
    "data",
    [
        b"definitely not an image",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,  # PNG signature, garbage body
        _encode(Image.new("RGB", (400, 400)), "JPEG")[:200],  # truncated
    ],
)
def test_undecodable_input_is_malformed(data):
    with pytest.raises(MalformedInputError):
        make_thumbnail(data, max_size=1024, quality=80)


# ---- image limits: refused from the header, before decoding ----


def test_huge_declared_dimensions_are_refused_before_decoding():
    # A tiny file declaring 20000x20000 pixels (a decompression bomb).
    original = _encode(Image.new("1", (20_000, 20_000)), "PNG")
    assert len(original) < 100_000

    with pytest.raises(ProcessingLimitExceeded, match="pixel limit"):
        make_thumbnail(original, max_size=1024, quality=80)


def test_degenerate_strip_over_the_side_limit_is_refused():
    # Few pixels in all, but absurdly long on one side.
    original = _encode(Image.new("1", (60_000, 2)), "PNG")

    with pytest.raises(ProcessingLimitExceeded, match="60000x2"):
        make_thumbnail(original, max_size=1024, quality=80, limits=ImageLimits(max_width=50_000))


def test_too_many_declared_pixels_are_refused_even_if_small_decoded():
    # Within the decoded-pixel limit at 1/8 scale, but over what's declared.
    original = _encode(Image.new("L", (12_000, 9_000)), "JPEG")

    with pytest.raises(ProcessingLimitExceeded, match="100000000-pixel limit"):
        make_thumbnail(original, max_size=1024, quality=80, limits=ImageLimits(max_declared_pixels=100_000_000))


@pytest.mark.parametrize("size", [(1_200, 1_200), (1_500, 1_500)], ids=["warning-zone", "over-twice"])
def test_pillows_own_bomb_guard_is_a_limit_not_a_crash(monkeypatch, size):
    """Pillow's guard (a warning up to twice its limit, an error over it),
    as `configure_pillow` sets it up, whatever our own limits say."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    original = _encode(Image.new("RGB", size), "PNG")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(ProcessingLimitExceeded, match="decompression-bomb"):
            make_thumbnail(original, max_size=1024, quality=80)


def test_pillow_bomb_guard_is_on_and_its_warning_is_an_error():
    ThumbnailHandler(
        storage=FakeObjectStore(), engine=None, outbox=None, max_size=1024, quality=80,
        limits=ImageLimits(max_declared_pixels=123_000_000),
    )

    assert Image.MAX_IMAGE_PIXELS == 123_000_000
    with pytest.raises(Image.DecompressionBombWarning):
        warnings.warn("bomb", Image.DecompressionBombWarning)
    ThumbnailHandler(storage=FakeObjectStore(), engine=None, outbox=None, max_size=1024, quality=80)


def test_formats_the_api_never_accepts_are_not_decoded():
    # Pillow could decode these, but no upload is one.
    for format in ("BMP", "TIFF"):
        with pytest.raises(MalformedInputError):
            make_thumbnail(_encode(Image.new("RGB", (64, 64)), format), max_size=1024, quality=80)


def test_only_the_first_frame_of_an_animation_is_used():
    frames = [Image.new("RGB", (64, 64), color) for color in ("red", "lime", "blue")]
    original = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:])

    thumbnail = _decode(make_thumbnail(original, max_size=32, quality=100))

    red, green, blue = thumbnail.convert("RGB").getpixel((16, 16))
    assert red > 200 and green < 60 and blue < 60


# ---- ThumbnailHandler ----


def _job(item_id: uuid.UUID, *, user_id: uuid.UUID = OWNER_ID, original_key: str = _ORIGINAL_KEY) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=user_id,
        item_type=ItemType.image,
        image=ImageRef(storage_key=original_key, content_type="image/png"),
    )


@pytest.fixture
def storage() -> FakeObjectStore:
    return FakeObjectStore({_ORIGINAL_KEY: _encode(Image.new("RGB", (2048, 1536), "teal"))})


@pytest.fixture
def analysis_queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def handler(engine, storage, analysis_queue) -> ThumbnailHandler:
    return ThumbnailHandler(
        storage=storage,
        engine=engine,
        outbox=outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}),
        max_size=1024,
        quality=80,
    )


async def test_stores_thumbnail_records_it_then_hands_off_to_analysis(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)

    await handler.handle(_job(item_id))

    key = thumbnail_key(item_id)
    assert _decode(storage.objects[key]).size == (1024, 768)
    assert storage.content_types[key] == "image/webp"
    assert await fetch_thumbnail_key(engine, item_id) == key
    # The analysis job points at the thumbnail, not the original.
    [job] = analysis_queue.published
    assert job.item_id == item_id
    assert job.image == ImageRef(storage_key=key, content_type="image/webp")


async def test_thumbnail_is_stored_under_its_owners_prefix(engine, storage, handler):
    """Next to the owner's original, in the same `users/{user_id}/`
    hierarchy — whatever layout the original itself was stored under."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)

    await handler.handle(_job(item_id))

    assert await fetch_thumbnail_key(engine, item_id) == f"users/{OWNER_ID}/thumbnails/{item_id}.webp"


async def test_legacy_original_key_gets_a_user_scoped_thumbnail(engine, storage, handler):
    item_id = uuid.uuid4()
    legacy_key = f"images/{item_id}.png"
    storage.objects[legacy_key] = storage.objects[_ORIGINAL_KEY]
    await insert_item(engine, item_id, status="processing", storage_key=legacy_key)

    await handler.handle(_job(item_id, original_key=legacy_key))

    assert await fetch_thumbnail_key(engine, item_id) == thumbnail_key(item_id)
    assert thumbnail_key(item_id) in storage.objects


async def test_job_for_another_users_item_records_nothing(engine, storage, analysis_queue, handler):
    """The job's user id picks the thumbnail's prefix, so it's checked
    against the item's owner in the database: a mismatched job (a bug, or a
    forged message) neither records a thumbnail under another user's prefix
    nor leaves one behind."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)
    other_user = uuid.uuid4()

    with pytest.raises(PermanentProcessingError):
        await handler.handle(_job(item_id, user_id=other_user))

    assert await fetch_thumbnail_key(engine, item_id) is None
    assert storage.bytes_served == {}
    assert storage_keys.thumbnail_key(other_user, item_id) not in storage.objects
    assert analysis_queue.published == []


async def test_rerun_is_idempotent(engine, storage, analysis_queue, handler):
    """A redelivered job overwrites the same thumbnail rather than adding
    another; the only repeat is the hand-off, which analysis tolerates."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)

    await handler.handle(_job(item_id))
    await handler.handle(_job(item_id))

    assert sorted(storage.objects) == sorted([_ORIGINAL_KEY, thumbnail_key(item_id)])
    assert await fetch_thumbnail_key(engine, item_id) == thumbnail_key(item_id)
    assert len(analysis_queue.published) == 2


async def test_item_already_deleted_is_not_processed(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()  # never inserted: as if deleted before the job ran

    with pytest.raises(PermanentProcessingError):
        await handler.handle(_job(item_id))

    assert storage.bytes_served == {}
    assert analysis_queue.published == []


async def test_item_deleted_meanwhile_discards_thumbnail(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)
    size = storage.size

    async def read_then_delete_item(key, **kwargs):
        result = await size(key, **kwargs)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM item_images WHERE item_id = :id"), {"id": str(item_id)})
            await conn.execute(text("DELETE FROM items WHERE id = :id"), {"id": str(item_id)})
        return result

    storage.size = read_then_delete_item

    await handler.handle(_job(item_id))

    assert thumbnail_key(item_id) not in storage.objects
    assert analysis_queue.published == []


async def test_original_is_read_to_disk_in_ranges_never_whole_into_memory(
    engine, analysis_queue, monkeypatch
):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)
    monkeypatch.setattr("stash_worker_core.storage.DOWNLOAD_CHUNK_BYTES", 64 * 1024)
    # Noise doesn't compress: a few hundred KB of PNG.
    original = _encode(Image.effect_noise((512, 512), 64).convert("RGB"))
    storage = FakeObjectStore({_ORIGINAL_KEY: original})

    async def no_download(*args, **kwargs):
        raise AssertionError("the original must not be downloaded into memory")

    storage.download = no_download
    handler = ThumbnailHandler(
        storage=storage,
        engine=engine,
        outbox=outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}),
        max_size=1024,
        quality=80,
    )

    await handler.handle(_job(item_id))

    assert storage.bytes_served[_ORIGINAL_KEY] == len(original) > 4 * 64 * 1024
    assert storage.largest_read[_ORIGINAL_KEY] == 64 * 1024
    assert await fetch_thumbnail_key(engine, item_id) == thumbnail_key(item_id)
    assert DOWNLOAD_CHUNK_BYTES == 8 * 1024 * 1024  # the real chunk, patched above


@pytest.mark.parametrize("data", [_encode(Image.new("RGB", (64, 64))), _CORRUPT_PNG], ids=["ok", "corrupt"])
async def test_temporary_file_is_removed_on_success_and_failure(engine, analysis_queue, monkeypatch, data):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)
    opened = []
    temporary_file = tempfile.TemporaryFile

    def tracked(*args, **kwargs):
        opened.append(temporary_file(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked)
    handler = ThumbnailHandler(
        storage=FakeObjectStore({_ORIGINAL_KEY: data}),
        engine=engine,
        outbox=outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}),
        max_size=1024,
        quality=80,
    )

    try:
        await handler.handle(_job(item_id))
    except MalformedInputError:
        pass

    assert len(opened) == 1 and opened[0].closed


async def test_image_over_a_limit_fails_once_without_retries(engine, analysis_queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    dead_letters = FakeDeadLetterQueue()
    thumbnail_queue = FakeJobQueue()
    bomb = _encode(Image.new("1", (20_000, 20_000)), "PNG")
    worker = Worker(
        queue=thumbnail_queue,
        dead_letters=dead_letters,
        engine=engine,
        handler=ThumbnailHandler(
            storage=FakeObjectStore({_ORIGINAL_KEY: bomb}),
            engine=engine,
            outbox=outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}),
            max_size=1024,
            quality=80,
        ),
    )

    await worker.process_message(_delivery(_job(item_id)))

    assert await fetch_status(engine, item_id) == "failed"
    assert [letter.delivery_count for letter in dead_letters.letters] == [1]
    # Pillow's own guard, as the handler sets it up, is what stops it here.
    assert "decompression-bomb limit" in dead_letters.letters[0].reason
    assert thumbnail_queue.retried == []
    assert analysis_queue.published == []


async def test_nothing_is_published_if_storing_fails(engine, storage, analysis_queue, handler):
    """Hand-off happens only after the thumbnail is safely stored."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)

    async def _failing_upload(key, data, *, content_type):
        raise ConnectionError("storage down")

    storage.upload = _failing_upload

    with pytest.raises(ConnectionError):
        await handler.handle(_job(item_id))
    assert analysis_queue.published == []
    assert await fetch_thumbnail_key(engine, item_id) is None


# ---- through the Worker ----


def _delivery(job: ProcessingJob) -> Delivery:
    return Delivery(message_id="1-0", receipt="1-0", delivery_count=1, raw_payload="{}", job=job)


async def test_corrupt_upload_fails_item_at_thumbnail_stage(engine, analysis_queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    dead_letters = FakeDeadLetterQueue()
    thumbnail_queue = FakeJobQueue()
    worker = Worker(
        queue=thumbnail_queue,
        dead_letters=dead_letters,
        engine=engine,
        handler=ThumbnailHandler(
            storage=FakeObjectStore({_ORIGINAL_KEY: b"\x89PNG\r\n\x1a\n corrupt"}),
            engine=engine,
            outbox=outbox_for(engine, {CONTENT_ANALYSIS_JOBS: analysis_queue}),
            max_size=1024,
            quality=80,
        ),
    )

    await worker.process_message(_delivery(_job(item_id)))

    # Straight to failed + dead letter, no retries, never reaches OpenAI.
    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1
    assert thumbnail_queue.retried == []
    assert analysis_queue.published == []


async def test_original_over_the_size_limit_fails_without_being_read(engine, analysis_queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    storage = FakeObjectStore({_ORIGINAL_KEY: _encode(Image.new("RGB", (64, 64)))})
    worker = Worker(
        queue=FakeJobQueue(),
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=ThumbnailHandler(
            storage=storage, engine=engine, outbox=outbox_for(engine), max_size=1024, quality=80, max_source_bytes=50
        ),
        max_attempts=5,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    await worker.process_message(Delivery("1-0", "1-0", 1, "{}", _job(item_id)))

    assert await fetch_status(engine, item_id) == "failed"
    assert storage.bytes_served == {}


# ---- the original comes from the database, pinned to its validated content ----


async def test_reads_the_original_the_database_records_not_the_one_the_job_names(
    engine, storage, analysis_queue, handler
):
    """A job's `image` is informational: a stale or forged key in it is
    never read."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", storage_key=_ORIGINAL_KEY)
    other_key = f"users/{uuid.uuid4()}/images/someone-elses.png"
    storage.objects[other_key] = _encode(Image.new("RGB", (10, 10), "red"))

    await handler.handle(_job(item_id, original_key=other_key))

    assert other_key not in storage.bytes_served
    assert storage.bytes_served[_ORIGINAL_KEY] == len(storage.objects[_ORIGINAL_KEY])
    assert _decode(storage.objects[thumbnail_key(item_id)]).size == (1024, 768)


async def test_original_replaced_after_validation_is_never_processed(engine, storage, analysis_queue, handler):
    """The item records the ETag of the content the API validated; other
    bytes at its key (however they got there) fail the item, unread."""
    item_id = uuid.uuid4()
    validated_etag = storage.etag_of(storage.objects[_ORIGINAL_KEY])
    await insert_item(
        engine, item_id, status="processing", storage_key=_ORIGINAL_KEY, content_etag=validated_etag
    )
    storage.objects[_ORIGINAL_KEY] = _encode(Image.new("RGB", (10, 10), "red"))

    with pytest.raises(PermanentProcessingError):
        await handler.handle(_job(item_id))

    assert thumbnail_key(item_id) not in storage.objects
    assert analysis_queue.published == []


async def test_original_with_its_validated_etag_is_processed(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()
    await insert_item(
        engine,
        item_id,
        status="processing",
        storage_key=_ORIGINAL_KEY,
        content_etag=storage.etag_of(storage.objects[_ORIGINAL_KEY]),
    )

    await handler.handle(_job(item_id))

    assert await fetch_thumbnail_key(engine, item_id) == thumbnail_key(item_id)
