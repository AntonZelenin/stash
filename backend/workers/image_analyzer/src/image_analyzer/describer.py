import base64
import json
from abc import ABC, abstractmethod

from stash_shared.log import get_logger, logged_call
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.openai_client import PERMANENT_OPENAI_ERRORS, build_openai_client

logger = get_logger(__name__)

_PROMPT = """\
You are indexing an image saved to a personal content library so that it can be found again later by searching.
Break what is clearly visible in the image into short semantic search chunks. Each chunk is embedded and matched \
against search queries on its own, so each one must stand alone as one coherent, searchable concept.

First, read the text in the image. Fill `visible_text` with every distinct piece of text you can actually read: \
signs, shop names, neon lettering, labels, captions, logos, screen and UI text, handwriting, speech bubbles. \
One entry per sign or text block, transcribed exactly as written, in its original script and language \
(Korean stays in Hangul, Japanese in kana/kanji, and so on): never translate, romanise or paraphrase it. \
If only part of a text is legible, give the legible part. Leave out text you cannot actually read, but never \
replace readable text with a description of it: "Korean and English signage" or "neon signs with text" instead \
of the words themselves is wrong. Leave `visible_text` empty only if the image has no readable text at all.

Then fill `chunks`, covering the important searchable content:
- the main subjects (people, animals, characters, objects) and what they are doing;
- the setting or scene, naming the broad scene type when apparent (city, street, beach, forest, room, event...);
- distinctive or important objects, clothing, colours and visual details;
- the style or medium when notable (photo, screenshot, anime, drawing, diagram...);
- the overall mood when it is reasonably apparent.

Chunk rules:
- give each main subject its own identity chunk: only who or what it is, with close alternative terms, e.g. \
"young woman, girl, female" or "golden retriever, dog, pet"; what it is doing, wearing or holding goes in \
other chunks ("upside down, headstand, breakdance pose"), never mixed into the identity chunk;
- name people and characters concretely (woman, man, girl, boy, child, old man...) whenever it is apparent; \
use "person" or "figure" only when it genuinely cannot be told;
- short meaningful phrases, not full sentences;
- one concept per chunk, keeping relationships where they matter, e.g. "dog running on beach" rather than \
separate "dog", "beach" and "running" chunks;
- include close alternative terms only when genuinely useful for search, e.g. "woman, girl, female"; \
no keyword stuffing and no repeating the same idea across chunks;
- the text itself goes in `visible_text`, not in `chunks`; a chunk may still say where text is \
(e.g. "neon shop signs on skyscrapers");
- leave out minor background elements unless they are distinctive or important to the scene;
- infer likely actions or events only when reasonably supported by the image;
- do not guess uncertain details: if something is ambiguous or not clearly visible, describe it more generally.

Write the chunks in English.
Aim for 4-10 chunks for a typical image: fewer for a simple image, more only for a genuinely busy one.

Example chunks for a photo: golden retriever, dog, pet;
dog running through shallow water;
sandy beach, ocean shoreline;
red frisbee near the dog;
bright sunny weather, blue sky.

Example chunks for a screenshot: software dashboard, analytics interface;
line chart showing traffic growth;
left navigation sidebar;
date range selector;
dark theme user interface.
"""

# Structured output: the model has to answer with exactly this JSON.
# `visible_text` comes first so the text is transcribed before anything is
# summarised: asked for inside the chunks, it got described ("Korean
# signage") rather than written out.
_OUTPUT_FORMAT = {
    "type": "json_schema",
    "name": "image_search_chunks",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "visible_text": {"type": "array", "items": {"type": "string"}},
            "chunks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["visible_text", "chunks"],
        "additionalProperties": False,
    },
}

# Far more than a busy image needs; only guard against a runaway answer.
_MAX_CHUNKS = 20
_MAX_TEXT_CHUNKS = 15
# Each transcribed text becomes its own chunk, labelled so its embedding
# reads as "the image says ..." rather than a bare word.
_TEXT_CHUNK_PREFIX = "text: "


class ImageDescriber(ABC):
    @abstractmethod
    async def describe(self, image: bytes, *, content_type: str) -> list[str]:
        """Describes `image` as short search chunks, each one searchable
        concept (never empty). Raises `PermanentProcessingError` for input
        that can never be described; any other exception is treated as
        transient."""
        ...


class OpenAIImageDescriber(ImageDescriber):
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float):
        self._client = build_openai_client(api_key=api_key, timeout_seconds=timeout_seconds)
        self._model = model

    async def describe(self, image: bytes, *, content_type: str) -> list[str]:
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
                                # Full resolution: small signage is
                                # unreadable when downscaled.
                                {"type": "input_image", "image_url": data_url, "detail": "high"},
                            ],
                        }
                    ],
                    text={"format": _OUTPUT_FORMAT},
                )
                call.update(response_status=response.status, output_chars=len(response.output_text or ""))
        except PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the image: {exc}") from exc

        chunks = parse_chunks(response.output_text or "")
        if not chunks:
            raise RuntimeError(f"OpenAI returned no search chunks (status={response.status!r})")
        return chunks


def parse_chunks(output: str) -> list[str]:
    """The search chunks in the model's JSON answer: its `chunks` (at most
    `_MAX_CHUNKS`), then each transcribed `visible_text` as a chunk of its
    own, "text: <as written>" (at most `_MAX_TEXT_CHUNKS`). Each is on one
    line with its whitespace collapsed (a generated description stores one
    chunk per line), without empty ones or case-insensitive repeats. An
    answer that isn't the expected JSON raises (as transient: another
    attempt may well get a valid one)."""
    try:
        answer = json.loads(output)
        raw_chunks = answer["chunks"]
        raw_texts = answer.get("visible_text", [])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("OpenAI returned search chunks that aren't the expected JSON") from exc
    if not isinstance(raw_chunks, list) or not isinstance(raw_texts, list):
        raise RuntimeError("OpenAI returned search chunks that aren't a list")
    seen: set[str] = set()
    chunks = _clean(raw_chunks, seen)[:_MAX_CHUNKS]
    texts = _clean(raw_texts, seen)[:_MAX_TEXT_CHUNKS]
    return chunks + [_TEXT_CHUNK_PREFIX + value for value in texts]


def _clean(values: list, seen: set[str]) -> list[str]:
    """`values`' strings with whitespace collapsed, leaving out empty ones
    and any already in `seen` (case-insensitively), which it adds to."""
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            cleaned.append(value)
    return cleaned
