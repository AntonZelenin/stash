import base64
from abc import ABC, abstractmethod

import openai
from openai import AsyncOpenAI

from content_analyzer.errors import PermanentProcessingError

_PROMPT = (
    "You are describing an image saved to a personal content library, so that it can be found again "
    "later by searching. Describe what is in the image: the main subjects, setting, notable objects, "
    "colors, activity and mood, and transcribe any clearly legible text. Write 2-5 plain sentences, "
    "no markdown, no preamble."
)

# 400/422 mean OpenAI looked at the request and rejected *this* input (e.g.
# an invalid/corrupt or oversized image, or a content-policy block) —
# retrying the same bytes won't change that. Everything else — 429, 5xx,
# timeouts, connection errors, and also auth/permission/not-found, which are
# deployment problems rather than the image's fault — is left to propagate
# as transient so the job is retried with backoff.
_PERMANENT_OPENAI_ERRORS = (openai.BadRequestError, openai.UnprocessableEntityError)


class ImageDescriber(ABC):
    @abstractmethod
    async def describe(self, image: bytes, *, content_type: str) -> str:
        """Returns a text description of `image`. Raises
        `PermanentProcessingError` for input that can never be described;
        any other exception is treated as transient."""
        ...


class OpenAIImageDescriber(ImageDescriber):
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float):
        # max_retries=0: the queue owns retries (with backoff that spans
        # redeliveries and survives restarts), so the SDK's own in-process
        # retries would only multiply the attempts against OpenAI.
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self._model = model

    async def describe(self, image: bytes, *, content_type: str) -> str:
        # Sent inline rather than as a URL: storage isn't publicly reachable
        # from OpenAI (MinIO locally is on the Docker network).
        data_url = f"data:{content_type};base64,{base64.b64encode(image).decode()}"
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _PROMPT},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            )
        except _PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the image: {exc}") from exc

        description = (response.output_text or "").strip()
        if not description:
            raise RuntimeError(f"OpenAI returned an empty description (status={response.status!r})")
        return description
