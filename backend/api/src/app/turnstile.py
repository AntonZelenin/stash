"""Cloudflare Turnstile: proof that a registration came from a person in a
browser, so accounts (and the per-user quotas that come with each) can't be
created by the thousand.

The frontend renders the Turnstile widget on the sign-up form and sends the
token it produces with the registration (`turnstile_token`). The token alone
proves nothing: it's checked here, server-side, with Cloudflare's siteverify
API and our secret key, and it must have been solved for the `register`
action, on one of our hostnames. Tokens are single-use and expire after
five minutes, which siteverify enforces (`timeout-or-duplicate`).

A missing, invalid, expired or reused token is rejected the same way. If
Cloudflare can't be reached, registration fails (closed): skipping the
check would reopen exactly what it closes.
"""

import uuid
from collections.abc import Callable
from functools import lru_cache

import httpx
from stash_shared.log import get_logger

from app.config import Settings, get_settings, get_turnstile_secret_key

logger = get_logger(__name__)

# The action the frontend renders the widget with (Cloudflare echoes it
# back), so a token solved for another form isn't accepted here.
REGISTER_ACTION = "register"


class TurnstileUnavailableError(Exception):
    """Cloudflare couldn't be asked (unreachable, an error, no secret
    configured): nothing is known about the token."""


class TurnstileVerifier:
    def __init__(
        self,
        *,
        enabled: bool,
        verify_url: str,
        timeout_seconds: float,
        allowed_hostnames: list[str],
        secret_key: Callable[[], str],
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """`secret_key` is called on each verification (it's fetched from
        Secrets Manager on first use, then cached there). `transport`
        replaces the network in tests."""
        self._enabled = enabled
        self._verify_url = verify_url
        self._timeout_seconds = timeout_seconds
        self._allowed_hostnames = set(allowed_hostnames)
        self._secret_key = secret_key
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "TurnstileVerifier":
        return cls(
            enabled=settings.turnstile_enabled,
            verify_url=settings.turnstile_verify_url,
            timeout_seconds=settings.turnstile_timeout_seconds,
            allowed_hostnames=settings.turnstile_allowed_hostnames,
            secret_key=get_turnstile_secret_key,
        )

    async def verify(self, token: str | None, *, remote_ip: str | None, action: str = REGISTER_ACTION) -> bool:
        """Whether `token` is a valid, unused Turnstile token for `action`
        on one of our hostnames. Always True when Turnstile is disabled.
        Raises `TurnstileUnavailableError` if Cloudflare can't say."""
        if not self._enabled:
            return True
        if not token:
            logger.info("Turnstile verification failed", reason="missing_token")
            return False

        try:
            secret_key = self._secret_key()
        except Exception as exc:
            raise TurnstileUnavailableError("The Turnstile secret key can't be read") from exc
        if not secret_key:
            raise TurnstileUnavailableError("No Turnstile secret key is configured")

        form = {
            "secret": secret_key,
            "response": token,
            # Cloudflare's retry-safety: the same key verifies once.
            "idempotency_key": str(uuid.uuid4()),
        }
        if remote_ip:
            form["remoteip"] = remote_ip
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = await client.post(self._verify_url, data=form)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Turnstile verification unavailable", error_type=type(exc).__name__)
            raise TurnstileUnavailableError("Cloudflare siteverify failed") from exc

        if not result.get("success"):
            logger.info("Turnstile verification failed", reason="rejected", error_codes=result.get("error-codes"))
            return False
        if result.get("action") != action:
            logger.info("Turnstile verification failed", reason="wrong_action", action=result.get("action"))
            return False
        if self._allowed_hostnames and result.get("hostname") not in self._allowed_hostnames:
            logger.info("Turnstile verification failed", reason="wrong_hostname", hostname=result.get("hostname"))
            return False
        return True


@lru_cache
def get_turnstile_verifier() -> TurnstileVerifier:
    return TurnstileVerifier.from_settings(get_settings())
