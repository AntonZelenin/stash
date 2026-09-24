import uuid

import pytest
from stash_shared.queue.base import FileRef, ImageRef, ItemType

from content_analyzer.items import start_attempt
from content_analyzer.sweeper import StaleItemSweeper
from conftest import FakeJobQueue, fetch_requeue_count, fetch_status, insert_item

_STALE_AFTER = 1800


@pytest.fixture
def queue() -> FakeJobQueue:
    """The thumbnail stage's queue: where an item without a thumbnail yet
    is re-published (most tests' items)."""
    return FakeJobQueue()


@pytest.fixture
def analysis_queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def document_queue() -> FakeJobQueue:
    return FakeJobQueue()


def _sweeper(engine, queue, analysis_queue, document_queue=None) -> StaleItemSweeper:
    return StaleItemSweeper(
        thumbnail_queue=queue,
        analysis_queue=analysis_queue,
        document_queue=document_queue or FakeJobQueue(),
        engine=engine,
        stale_after_seconds=_STALE_AFTER,
        max_requeues=3,
        interval_seconds=60,
    )


@pytest.fixture
def sweeper(engine, queue, analysis_queue, document_queue) -> StaleItemSweeper:
    return _sweeper(engine, queue, analysis_queue, document_queue)


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_stale_image_item_is_republished_with_image_ref(sweeper, engine, queue, status):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status=status, age_seconds=_STALE_AFTER + 60)

    await sweeper.sweep_once()

    [job] = queue.published
    assert job.item_id == item_id
    assert job.item_type == ItemType.image
    assert job.image == ImageRef(storage_key="images/cat.png", content_type="image/png")
    assert await fetch_requeue_count(engine, item_id) == 1
    assert await fetch_status(engine, item_id) == status


async def test_requeue_resets_staleness_clock(sweeper, engine, queue):
    """A second sweep right after must not publish the same item again."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, age_seconds=_STALE_AFTER + 60)

    await sweeper.sweep_once()
    await sweeper.sweep_once()

    assert len(queue.published) == 1


async def test_fresh_items_are_left_alone(sweeper, engine, queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", age_seconds=_STALE_AFTER - 60)

    await sweeper.sweep_once()

    assert queue.published == []
    assert await fetch_requeue_count(engine, item_id) == 0


async def test_attempt_start_keeps_a_live_item_fresh(sweeper, engine, queue):
    """Each processing attempt refreshes the clock, so an item whose job is
    still being retried never looks stale."""
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status="processing", age_seconds=_STALE_AFTER + 60)

    await start_attempt(engine, item_id)
    await sweeper.sweep_once()

    assert queue.published == []


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_finished_items_are_never_swept(sweeper, engine, queue, status):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, status=status, age_seconds=_STALE_AFTER * 10)

    await sweeper.sweep_once()

    assert queue.published == []
    assert await fetch_status(engine, item_id) == status


async def test_item_is_failed_after_max_requeues(sweeper, engine, queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, age_seconds=_STALE_AFTER + 60, requeue_count=3)

    await sweeper.sweep_once()

    assert queue.published == []
    assert await fetch_status(engine, item_id) == "failed"


@pytest.mark.parametrize("item_type", ["text", "link"])
async def test_non_image_items_are_never_swept(sweeper, engine, queue, item_type):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, item_type=item_type, age_seconds=_STALE_AFTER + 60)

    await sweeper.sweep_once()

    assert queue.published == []
    assert await fetch_status(engine, item_id) == "pending"


async def test_concurrent_sweepers_publish_once(engine, queue, analysis_queue):
    item_id = uuid.uuid4()
    await insert_item(engine, item_id, age_seconds=_STALE_AFTER + 60)
    sweepers = [_sweeper(engine, queue, analysis_queue) for _ in range(2)]

    for sweeper in sweepers:
        await sweeper.sweep_once()

    assert len(queue.published) == 1


async def test_item_with_thumbnail_resumes_at_content_analysis(sweeper, engine, queue, analysis_queue):
    """It got stuck after the thumbnail stage, so redoing that stage would be
    wasted work: it goes straight to analysis, pointed at the thumbnail."""
    item_id = uuid.uuid4()
    await insert_item(
        engine, item_id, status="processing", age_seconds=_STALE_AFTER + 60, thumbnail_key=f"thumbnails/{item_id}.webp"
    )

    await sweeper.sweep_once()

    assert queue.published == []
    [job] = analysis_queue.published
    assert job.image == ImageRef(storage_key=f"thumbnails/{item_id}.webp", content_type="image/webp")


async def test_stale_file_item_is_republished_to_document_analysis(
    sweeper, engine, queue, analysis_queue, document_queue
):
    item_id = uuid.uuid4()
    await insert_item(
        engine,
        item_id,
        item_type="file",
        status="pending",
        age_seconds=_STALE_AFTER + 60,
        file=(f"files/{item_id}.pdf", "application/pdf", "Report.pdf"),
    )

    await sweeper.sweep_once()

    assert queue.published == [] and analysis_queue.published == []
    [job] = document_queue.published
    assert job.item_type == ItemType.file
    assert job.file == FileRef(storage_key=f"files/{item_id}.pdf", content_type="application/pdf", filename="Report.pdf")
