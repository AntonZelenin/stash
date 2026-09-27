from uuid import UUID

from stash_shared import storage_keys

_USER = UUID("11111111-1111-4111-8111-111111111111")
_OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")
_ITEM = UUID("33333333-3333-4333-8333-333333333333")
_OBJECT = storage_keys.new_object_id()


def test_keys_are_scoped_under_the_owner():
    assert storage_keys.image_key(_USER, _ITEM, _OBJECT, ".png") == f"users/{_USER}/images/{_ITEM}/{_OBJECT}.png"
    assert storage_keys.file_key(_USER, _ITEM, _OBJECT, ".pdf") == f"users/{_USER}/files/{_ITEM}/{_OBJECT}.pdf"
    assert storage_keys.thumbnail_key(_USER, _ITEM) == f"users/{_USER}/thumbnails/{_ITEM}.webp"


def test_unrecognized_file_has_no_extension():
    assert storage_keys.file_key(_USER, _ITEM, _OBJECT, "") == f"users/{_USER}/files/{_ITEM}/{_OBJECT}"


def test_different_users_never_share_a_key():
    assert storage_keys.file_key(_USER, _ITEM, _OBJECT, ".pdf") != storage_keys.file_key(
        _OTHER_USER, _ITEM, _OBJECT, ".pdf"
    )
    assert storage_keys.thumbnail_key(_USER, _ITEM) != storage_keys.thumbnail_key(_OTHER_USER, _ITEM)
    assert storage_keys.staging_key(_USER, _ITEM) != storage_keys.staging_key(_OTHER_USER, _ITEM)


def test_thumbnail_key_is_deterministic():
    """Re-running a thumbnail job overwrites the same object."""
    assert storage_keys.thumbnail_key(_USER, _ITEM) == storage_keys.thumbnail_key(_USER, _ITEM)


def test_staging_keys_are_disjoint_from_canonical_ones():
    """The lifecycle rule and the pre-signed-write policy both rely on it."""
    staging = storage_keys.staging_key(_USER, _ITEM)
    assert storage_keys.is_staging_key(staging)
    for key in (
        storage_keys.image_key(_USER, _ITEM, _OBJECT, ".png"),
        storage_keys.file_key(_USER, _ITEM, _OBJECT, ".pdf"),
        storage_keys.thumbnail_key(_USER, _ITEM),
        f"users/{_USER}/images/{_ITEM}.png",  # the legacy layout
    ):
        assert not storage_keys.is_staging_key(key)


def test_object_ids_are_fresh_and_key_safe():
    ids = {storage_keys.new_object_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(object_id) == 32 and object_id.isalnum() for object_id in ids)
