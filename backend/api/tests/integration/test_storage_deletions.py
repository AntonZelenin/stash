"""Pending object deletions (`app.storage.deletions`): recorded with the
database change, carried out by drains, retried until they succeed."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage import deletions
from app.storage.deletions import (
    StorageDeletionDrainer,
    schedule_key_deletion,
    schedule_prefix_deletion,
    storage_deletions,
)
from conftest import FakeObjectStorage

_USER = uuid.UUID("11111111-1111-4111-8111-111111111111")


async def _pending(session: AsyncSession) -> dict[str, int]:
    session.expire_all()
    rows = (await session.execute(select(storage_deletions))).all()
    return {row.target: row.attempts for row in rows}


@pytest.mark.parametrize("prefix", [f"users/{_USER}/", f"uploads/{_USER}/"])
async def test_a_users_prefixes_can_be_scheduled(session: AsyncSession, prefix: str):
    await schedule_prefix_deletion(session, prefix)
    await session.commit()

    assert await _pending(session) == {prefix: 0}


@pytest.mark.parametrize(
    "prefix",
    ["", "users/", "uploads/", f"users/{_USER}", f"users/{_USER}/images/", "users/not-a-uuid/", f"other/{_USER}/"],
)
async def test_nothing_but_one_users_area_can_be_scheduled_as_a_prefix(session: AsyncSession, prefix: str):
    """No bug can schedule the deletion of the bucket, or of everyone's
    objects."""
    with pytest.raises(ValueError):
        await schedule_prefix_deletion(session, prefix)


@pytest.mark.parametrize("key", ["", f"users/{_USER}/"])
async def test_a_key_must_name_an_object(session: AsyncSession, key: str):
    with pytest.raises(ValueError):
        await schedule_key_deletion(session, key)


async def test_nothing_is_deleted_unless_the_transaction_commits(
    session: AsyncSession, storage: FakeObjectStorage
):
    storage.uploads[f"users/{_USER}/a"] = (b"a", "text/plain")
    await schedule_prefix_deletion(session, f"users/{_USER}/")
    await session.rollback()

    assert await StorageDeletionDrainer(session.bind, storage).drain() == 0
    assert f"users/{_USER}/a" in storage.uploads


async def test_a_large_prefix_is_deleted_over_several_drains(
    session: AsyncSession, storage: FakeObjectStorage, monkeypatch
):
    monkeypatch.setattr(deletions, "_MAX_OBJECTS_PER_PREFIX", 2)
    for name in "abcde":
        storage.uploads[f"users/{_USER}/{name}"] = (b"x", "text/plain")
    await schedule_prefix_deletion(session, f"users/{_USER}/")
    await session.commit()
    drainer = StorageDeletionDrainer(session.bind, storage)

    assert await drainer.drain() == 0
    # Progress, not a failure.
    assert await _pending(session) == {f"users/{_USER}/": 0}
    assert len(storage.uploads) == 3
    assert await drainer.drain() == 0
    assert await drainer.drain() == 1

    assert storage.uploads == {}
    assert await _pending(session) == {}


async def test_a_deletion_that_keeps_failing_doesnt_hold_up_the_others(
    session: AsyncSession, storage: FakeObjectStorage, monkeypatch
):
    delete = storage.delete

    async def delete_unless_stuck(*, key: str) -> None:
        if key == "stuck":
            raise PermissionError("AccessDenied")
        await delete(key=key)

    monkeypatch.setattr(storage, "delete", delete_unless_stuck)
    storage.uploads["fine"] = (b"x", "text/plain")
    await schedule_key_deletion(session, "stuck")
    await session.commit()
    await schedule_key_deletion(session, "fine")
    await session.commit()
    drainer = StorageDeletionDrainer(session.bind, storage)

    # The first drain stops at the failure...
    assert await drainer.drain() == 0
    assert await _pending(session) == {"stuck": 1, "fine": 0}
    # ...after which the failing deletion goes to the back.
    assert await drainer.drain() == 1

    assert "fine" not in storage.uploads
    assert await _pending(session) == {"stuck": 2}
