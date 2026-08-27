from app.auth.security import generate_token, hash_password, hash_token, verify_password


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


def test_generate_token_returns_unique_high_entropy_values():
    tokens = {generate_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 32 for token in tokens)


def test_hash_token_is_deterministic_and_does_not_return_plaintext():
    token = generate_token()

    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token


def test_hash_token_differs_for_different_tokens():
    assert hash_token(generate_token()) != hash_token(generate_token())
