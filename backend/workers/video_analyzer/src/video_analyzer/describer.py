import base64
import json
from abc import ABC, abstractmethod

from stash_shared.log import get_logger, logged_call
from stash_worker_core.chunks import clean_chunks
from stash_worker_core.errors import PermanentProcessingError
from stash_worker_core.openai_client import PERMANENT_OPENAI_ERRORS, build_openai_client

from video_analyzer.frames import Frame

logger = get_logger(__name__)

_INSTRUCTIONS = """\
You are indexing a video saved to a personal content library so that it can be found again later by searching.
You are given still frames sampled from ONE video, in chronological order, each labelled with its time in the \
video. They are samples spread evenly across the whole video, not separate images: describe the video as a whole. \
There is no audio; describe only what can be seen.

Fill `summary` with one plain sentence saying what the video shows overall: its main subjects, what happens and \
where, e.g. "A man cooks pasta in a home kitchen, then serves it at a table.", "Three women wash a blue sports \
car in a car-wash bay." or "Dashcam footage of a car driving along a coastal highway at sunset."

Fill `visible_text` with every distinct piece of text you can actually read in any frame: signs, shop names, \
labels, captions, subtitles, titles, logos, screen and UI text, handwriting. One entry per sign or text block, \
transcribed exactly as written, in its original script and language: never translate, romanise or paraphrase it. \
Give each text once, however many frames show it. Leave out text you cannot actually read, but never replace \
readable text with a description of it. Leave `visible_text` empty if no frame has readable text.

Then fill `chunks` with short semantic search chunks covering the important searchable content of the whole video:
- the main subjects (people, animals, characters, objects): one identity chunk each, only who or what it is with \
close alternative terms, e.g. "young woman, girl, female" or "golden retriever, dog, pet";
- the main activities and actions, e.g. "dog catching a frisbee", "person skateboarding down a ramp";
- the setting and environment, naming the broad scene type (city street, beach, kitchen, office, stadium...);
- meaningful changes across the video: a new location, a new activity, day turning to night, e.g. \
"moves from the street into a cafe";
- distinctive objects, clothing, vehicles, colours and visual details;
- the kind of footage when notable (phone video, screen recording, animation, dashcam, gameplay, slideshow...).

Rules:
- describe only what the frames clearly show; if something is ambiguous, describe it more generally;
- name people concretely by what is visibly apparent (woman, man, girl, boy, child, old man, group of women...) \
whenever it is apparent, in the summary and the chunks alike; use "person", "people" or "adults" only when it \
genuinely cannot be told;
- never say who someone is: no names of people unless written on screen, and never guess specific \
locations, events or dates the frames don't make plain; describe them instead ("woman in a suit speaking at a \
podium", not who she is);
- the frames are samples, so don't invent what happens between them;
- short meaningful phrases, one concept per chunk, keeping relationships where they matter;
- concrete, search-friendly words; close alternative terms only where genuinely useful, no keyword stuffing, \
and no repeating the same idea across chunks;
- the text itself goes in `visible_text`, not in `chunks` or `summary`.

Write `summary` and `chunks` in English.
Aim for 5-15 chunks: fewer for a simple video, more only for a genuinely varied one.
"""

# Structured output: the model has to answer with exactly this JSON.
# `visible_text` before `chunks`, as for images: asked for inside the
# chunks, text got described rather than written out.
_OUTPUT_FORMAT = {
    "type": "json_schema",
    "name": "video_search_chunks",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "visible_text": {"type": "array", "items": {"type": "string"}},
            "chunks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "visible_text", "chunks"],
        "additionalProperties": False,
    },
}

# Only guards against a runaway answer.
_MAX_CHUNKS = 20
_MAX_TEXT_CHUNKS = 15
_TEXT_CHUNK_PREFIX = "text: "


class VideoDescriber(ABC):
    @abstractmethod
    async def describe(self, frames: list[Frame], *, duration_seconds: float | None) -> list[str]:
        """Describes the video `frames` were sampled from (in order) as one
        description: short search chunks, the first a one-sentence summary
        of the whole video (never empty). Raises `PermanentProcessingError`
        for input that can never be described; any other exception is
        treated as transient."""
        ...


class OpenAIVideoDescriber(VideoDescriber):
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float):
        self._client = build_openai_client(api_key=api_key, timeout_seconds=timeout_seconds)
        self._model = model

    async def describe(self, frames: list[Frame], *, duration_seconds: float | None) -> list[str]:
        length = f"The video is {_timestamp(duration_seconds)} long." if duration_seconds else "Its length is unknown."
        content: list[dict] = [{"type": "input_text", "text": f"{length} {len(frames)} frames, in order:"}]
        for number, frame in enumerate(frames, start=1):
            content.append({"type": "input_text", "text": f"Frame {number} at {_timestamp(frame.time_seconds)}:"})
            # Inline, like images: storage isn't reachable from OpenAI.
            # Frames are already scaled down (`FrameLimits`), so full
            # detail costs a few tiles each and keeps signage legible.
            data_url = "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode()
            content.append({"type": "input_image", "image_url": data_url, "detail": "high"})
        try:
            with logged_call(
                logger,
                "openai.responses",
                purpose="video_description",
                model=self._model,
                frame_count=len(frames),
                input_bytes=sum(len(frame.jpeg) for frame in frames),
            ) as call:
                response = await self._client.responses.create(
                    model=self._model,
                    instructions=_INSTRUCTIONS,
                    input=[{"role": "user", "content": content}],
                    text={"format": _OUTPUT_FORMAT},
                    # The video is the user's: OpenAI keeps no stored copy
                    # of the response (nothing here reads one back).
                    store=False,
                )
                call.update(response_status=response.status, output_chars=len(response.output_text or ""))
        except PERMANENT_OPENAI_ERRORS as exc:
            raise PermanentProcessingError(f"OpenAI rejected the video frames: {exc}") from exc

        chunks = parse_chunks(response.output_text or "")
        if not chunks:
            raise RuntimeError(f"OpenAI returned no search chunks (status={response.status!r})")
        return chunks


def parse_chunks(output: str) -> list[str]:
    """The description in the model's JSON answer, as search chunks: its
    `summary` first, then its `chunks` (at most `_MAX_CHUNKS`), then each
    transcribed `visible_text` as "text: <as written>" (at most
    `_MAX_TEXT_CHUNKS`). Each on one line, without empty ones or
    case-insensitive repeats. An answer that isn't the expected JSON raises
    (as transient: another attempt may well get a valid one)."""
    try:
        answer = json.loads(output)
        summary = answer.get("summary", "")
        raw_chunks = answer["chunks"]
        raw_texts = answer.get("visible_text", [])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("OpenAI returned a video description that isn't the expected JSON") from exc
    if not isinstance(raw_chunks, list) or not isinstance(raw_texts, list):
        raise RuntimeError("OpenAI returned search chunks that aren't a list")
    seen: set[str] = set()
    head = clean_chunks([summary], seen)
    chunks = clean_chunks(raw_chunks, seen)[:_MAX_CHUNKS]
    texts = clean_chunks(raw_texts, seen)[:_MAX_TEXT_CHUNKS]
    return head + chunks + [_TEXT_CHUNK_PREFIX + value for value in texts]


def _timestamp(seconds: float) -> str:
    """`m:ss` (or `h:mm:ss`), e.g. 0:07, 12:30, 1:02:03."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
