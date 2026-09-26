import base64
from abc import ABC, abstractmethod

from stash_shared.log import get_logger, logged_call
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.openai_client import PERMANENT_OPENAI_ERRORS, build_openai_client

logger = get_logger(__name__)

_PROMPT = (
    "You are describing an image saved to a personal content library, so that it can be found again "
    "later by searching. Describe what is in the image: the main subjects, setting, notable objects, "
    "colors, activity and mood, and transcribe any clearly legible text. Write 2-5 plain sentences "
    "in English (transcribed text stays as written), no markdown, no preamble."
)

class ImageDescriber(ABC):
    @abstractmethod
    async def describe(self, image: bytes, *, content_type: str) -> str:
        """Returns a text description of `image`. Raises
        `PermanentProcessingError` for input that can never be described;
        any other exception is treated as transient."""
        ...


class OpenAIImageDescriber(ImageDescriber):
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float):
        self._client = build_openai_client(api_key=api_key, timeout_seconds=timeout_seconds)
        self._model = model

    async def describe(self, image: bytes, *, content_type: str) -> str:
        # Sent inline rather than as a URL: storage isn't publicly reachable
        # from OpenAI (MinIO locally is on the Docker network).
        data_url = f"data:{content_type};base64,{base64.b64encode(image).decode()}"
        try:
            with logged_call(
                logger, "openai.responses", purpose="image_description", model=self._model, input_bytes=len(image)
            ) as call:
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
                call.update(response_status=response.status, output_chars=len(response.output_text or ""))
        except PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the image: {exc}") from exc

        description = (response.output_text or "").strip()
        if not description:
            raise RuntimeError(f"OpenAI returned an empty description (status={response.status!r})")
        return description
