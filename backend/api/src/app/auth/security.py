import hashlib
import secrets

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Raised by bcrypt for malformed/oversized input; treat as no match.
        return False


def generate_token() -> str:
    """A high-entropy opaque bearer token. Only its hash is ever persisted."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Tokens are already uniformly random and high-entropy, unlike passwords,
    # so a fast, unsalted hash is sufficient here (no bcrypt needed).
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
