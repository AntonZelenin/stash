from uuid import uuid4

import jwt
import pytest

from app.auth.security import create_access_token, decode_access_token, hash_password, verify_password


def test_hash_password_does_not_return_plaintext():
    assert hash_password("correct-horse") != "correct-horse"


def test_verify_password_accepts_matching_password():
    password_hash = hash_password("correct-horse")

    assert verify_password("correct-horse", password_hash) is True


def test_verify_password_rejects_wrong_password():
    password_hash = hash_password("correct-horse")

    assert verify_password("wrong-password", password_hash) is False


def test_verify_password_rejects_oversized_password_instead_of_raising():
    password_hash = hash_password("correct-horse")

    assert verify_password("x" * 100, password_hash) is False


def test_access_token_roundtrips_user_id():
    user_id = uuid4()

    token = create_access_token(user_id)

    assert decode_access_token(token) == user_id


def test_decode_access_token_rejects_tampered_token():
    token = create_access_token(uuid4())

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "tampered")
