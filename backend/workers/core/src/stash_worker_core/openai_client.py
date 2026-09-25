"""OpenAI setup shared by the image and document describers."""

import openai
from openai import AsyncOpenAI

# 400/422 mean OpenAI looked at the request and rejected *this* input (e.g.
# an invalid/corrupt or oversized image, or a content-policy block) —
# retrying the same input won't change that. Everything else — 429, 5xx,
# timeouts, connection errors, and also auth/permission/not-found, which are
# deployment problems rather than the input's fault — is left to propagate
# as transient so the job is retried with backoff.
PERMANENT_OPENAI_ERRORS = (openai.BadRequestError, openai.UnprocessableEntityError)


def build_openai_client(*, api_key: str, timeout_seconds: float) -> AsyncOpenAI:
    # max_retries=0: the queue owns retries (with backoff that spans
    # redeliveries and survives restarts), so the SDK's own in-process
    # retries would only multiply the attempts against OpenAI.
    return AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
