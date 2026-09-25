from uuid import UUID

from stash_shared import storage_keys

_USER = UUID("11111111-1111-4111-8111-111111111111")
_OTHER_USER = UUID("22222222-2222-4222-8222-222222222222")
_ITEM = UUID("33333333-3333-4333-8333-333333333333")


def test_keys_are_scoped_under_the_owner():
    assert storage_keys.image_key(_USER, _ITEM, ".png") == f"users/{_USER}/images/{_ITEM}.png"
    assert storage_keys.file_key(_USER, _ITEM, ".pdf") == f"users/{_USER}/files/{_ITEM}.pdf"
    assert storage_keys.thumbnail_key(_USER, _ITEM) == f"users/{_USER}/thumbnails/{_ITEM}.webp"


def test_unrecognized_file_has_no_extension():
    assert storage_keys.file_key(_USER, _ITEM, "") == f"users/{_USER}/files/{_ITEM}"


def test_different_users_never_share_a_key():
    assert storage_keys.file_key(_USER, _ITEM, ".pdf") != storage_keys.file_key(_OTHER_USER, _ITEM, ".pdf")
    assert storage_keys.thumbnail_key(_USER, _ITEM) != storage_keys.thumbnail_key(_OTHER_USER, _ITEM)


def test_thumbnail_key_is_deterministic():
    """Re-running a thumbnail job overwrites the same object."""
    assert storage_keys.thumbnail_key(_USER, _ITEM) == storage_keys.thumbnail_key(_USER, _ITEM)
