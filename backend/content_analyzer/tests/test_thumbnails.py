import io
import uuid

import pytest
from PIL import Image
from stash_shared.queue.base import Delivery, ImageRef, ItemType, ProcessingJob

from content_analyzer.analysis import ContentAnalysisHandler
from content_analyzer.errors import PermanentProcessingError
from content_analyzer.thumbnails import ThumbnailHandler, make_thumbnail, thumbnail_key
from content_analyzer.worker import Worker
from conftest import (
    FakeDeadLetterQueue,
    FakeJobQueue,
    FakeObjectStore,
    fetch_descriptions,
    fetch_status,
    fetch_thumbnail_key,
    insert_item,
)

_ORIGINAL_KEY = "images/original.png"


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


@pytest.mark.parametrize(
    "data",
    [
        b"definitely not an image",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,  # PNG signature, garbage body
        _encode(Image.new("RGB", (400, 400)), "JPEG")[:200],  # truncated
    ],
)
def test_undecodable_input_is_permanent(data):
    with pytest.raises(PermanentProcessingError):
        make_thumbnail(data, max_size=1024, quality=80)


# ---- ThumbnailHandler ----


def _job(item_id: uuid.UUID) -> ProcessingJob:
    return ProcessingJob(
        item_id=item_id,
        user_id=uuid.uuid4(),
        item_type=ItemType.image,
        image=ImageRef(storage_key=_ORIGINAL_KEY, content_type="image/png"),
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
        storage=storage, engine=engine, analysis_queue=analysis_queue, max_size=1024, quality=80
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


async def test_item_deleted_meanwhile_discards_thumbnail(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()  # never inserted: as if deleted before we recorded the thumbnail

    await handler.handle(_job(item_id))

    assert thumbnail_key(item_id) not in storage.objects
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


# ---- both stages together ----


class _RecordingDescriber:
    def __init__(self):
        self.received: list[tuple[bytes, str]] = []

    async def describe(self, image: bytes, *, content_type: str) -> str:
        self.received.append((image, content_type))
        return "A teal rectangle."


def _delivery(job: ProcessingJob) -> Delivery:
    return Delivery(receipt="1-0", delivery_count=1, raw_payload="{}", job=job)


async def test_pipeline_sends_the_thumbnail_to_the_describer(engine, storage, analysis_queue, handler):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, storage_key=_ORIGINAL_KEY)
    thumbnail_queue = FakeJobQueue()
    describer = _RecordingDescriber()

    thumbnail_worker = Worker(
        queue=thumbnail_queue, dead_letters=FakeDeadLetterQueue(), engine=engine, handler=handler
    )
    analysis_worker = Worker(
        queue=analysis_queue,
        dead_letters=FakeDeadLetterQueue(),
        engine=engine,
        handler=ContentAnalysisHandler(storage=storage, describer=describer, engine=engine),
    )

    await thumbnail_worker.handle_delivery(_delivery(_job(item_id)))
    # Between stages the item is still in flight, not finished.
    assert await fetch_status(engine, item_id) == "processing"
    [analysis_job] = analysis_queue.published
    await analysis_worker.handle_delivery(_delivery(analysis_job))

    [(sent_bytes, sent_type)] = describer.received
    assert sent_bytes == storage.objects[thumbnail_key(item_id)]
    assert sent_bytes != storage.objects[_ORIGINAL_KEY]
    assert sent_type == "image/webp"
    assert await fetch_status(engine, item_id) == "completed"
    assert await fetch_descriptions(engine, item_id) == ["A teal rectangle."]
    assert len(thumbnail_queue.acked) == 1
    assert len(analysis_queue.acked) == 1


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
            analysis_queue=analysis_queue,
            max_size=1024,
            quality=80,
        ),
    )

    await worker.handle_delivery(_delivery(_job(item_id)))

    # Straight to failed + dead letter, no retries, never reaches OpenAI.
    assert await fetch_status(engine, item_id) == "failed"
    assert len(dead_letters.letters) == 1
    assert thumbnail_queue.retried == []
    assert analysis_queue.published == []
