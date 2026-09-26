import json
from types import SimpleNamespace

import openai
import pytest

try:  # the transport library the installed openai SDK builds its errors on
    import httpx2 as httpx
except ImportError:
    import httpx

from stash_worker_core.errors import PermanentProcessingError

from image_analyzer.describer import OpenAIImageDescriber, parse_chunks


def _status_error(cls: type[openai.APIStatusError], status_code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    return cls("boom", response=httpx.Response(status_code, request=request), body=None)


def _describer_raising(monkeypatch, exc: Exception) -> OpenAIImageDescriber:
    describer = OpenAIImageDescriber(api_key="test", model="test-model", timeout_seconds=1)

    async def _create(**kwargs):
        raise exc

    monkeypatch.setattr(describer._client.responses, "create", _create)
    return describer


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(openai.BadRequestError, 400),
        _status_error(openai.UnprocessableEntityError, 422),
    ],
)
async def test_rejected_input_is_permanent(monkeypatch, exc):
    describer = _describer_raising(monkeypatch, exc)

    with pytest.raises(PermanentProcessingError):
        await describer.describe(b"bytes", content_type="image/png")


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.InternalServerError, 503),
        openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.test")),
        openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.test")),
    ],
)
async def test_retryable_errors_propagate_as_transient(monkeypatch, exc):
    describer = _describer_raising(monkeypatch, exc)

    with pytest.raises(type(exc)):
        await describer.describe(b"bytes", content_type="image/png")


def _describer_answering(monkeypatch, output_text: str, requests: list | None = None) -> OpenAIImageDescriber:
    describer = OpenAIImageDescriber(api_key="test", model="test-model", timeout_seconds=1)

    async def _create(**kwargs):
        if requests is not None:
            requests.append(kwargs)
        return SimpleNamespace(status="completed", output_text=output_text)

    monkeypatch.setattr(describer._client.responses, "create", _create)
    return describer


async def test_returns_the_search_chunks_the_model_answers_with(monkeypatch):
    requests = []
    answer = json.dumps(
        {"chunks": ["anime woman, girl, female", "cyberpunk city, futuristic urban environment", "sign reading 출구"]}
    )
    describer = _describer_answering(monkeypatch, answer, requests)

    chunks = await describer.describe(b"bytes", content_type="image/webp")

    assert chunks == ["anime woman, girl, female", "cyberpunk city, futuristic urban environment", "sign reading 출구"]
    # Asked for exactly that JSON shape, with the image inline.
    [request] = requests
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["input"][0]["content"][1]["image_url"].startswith("data:image/webp;base64,")


def test_chunks_are_one_line_each_without_blanks_or_repeats():
    answer = json.dumps({"chunks": ["  grey cat,\n kitten ", "", "   ", "Grey Cat, Kitten", "sofa", 3]})

    assert parse_chunks(answer) == ["grey cat, kitten", "sofa"]


def test_runaway_answers_are_capped():
    assert len(parse_chunks(json.dumps({"chunks": [f"chunk {i}" for i in range(50)]}))) == 20


@pytest.mark.parametrize("output_text", ["", "not json", '["a JSON array"]', '{"chunks": "one string"}', '{"chunks": []}'])
async def test_answer_without_usable_chunks_is_transient(monkeypatch, output_text):
    """Another attempt may well get a valid answer."""
    describer = _describer_answering(monkeypatch, output_text)

    with pytest.raises(RuntimeError):
        await describer.describe(b"bytes", content_type="image/png")


def test_each_transcribed_text_is_a_chunk_of_its_own_as_written():
    answer = json.dumps(
        {
            "visible_text": ["출구", "NEON  CITY", "neon city", "", "東京"],
            "chunks": ["cyberpunk city, futuristic urban environment", "neon shop signs on skyscrapers"],
        }
    )

    assert parse_chunks(answer) == [
        "cyberpunk city, futuristic urban environment",
        "neon shop signs on skyscrapers",
        "text: 출구",
        "text: NEON CITY",
        "text: 東京",
    ]


async def test_asks_for_the_text_first_and_the_image_at_full_resolution(monkeypatch):
    requests = []
    describer = _describer_answering(monkeypatch, json.dumps({"visible_text": [], "chunks": ["cat"]}), requests)

    await describer.describe(b"bytes", content_type="image/webp")

    [request] = requests
    schema = request["text"]["format"]["schema"]
    assert list(schema["properties"]) == ["visible_text", "chunks"]
    assert request["input"][0]["content"][1]["detail"] == "high"
