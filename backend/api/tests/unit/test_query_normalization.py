import json
from types import SimpleNamespace

import pytest

from app.query_normalization import (
    INSTRUCTIONS,
    MAX_EXPANSIONS,
    NormalizedQuery,
    OpenAIQueryNormalizer,
    QueryNormalizationError,
)


class FakeResponses:
    """`client.responses`: answers every call with `output_text`."""

    def __init__(self, output_text: str, status: str = "completed"):
        self.output_text = output_text
        self.status = status
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status, output_text=self.output_text)


def answer(query: str, expansions: list[str] | None = None) -> str:
    """The model's structured answer."""
    return json.dumps({"query": query, "expansions": expansions or []}, ensure_ascii=False)


def normalizer_answering(output_text: str, status: str = "completed") -> tuple[OpenAIQueryNormalizer, FakeResponses]:
    responses = FakeResponses(output_text, status)
    client = SimpleNamespace(responses=responses)
    return OpenAIQueryNormalizer(model="gpt-5-nano", timeout_seconds=5, client=client), responses


async def test_sends_the_query_as_input_with_the_rewrite_instructions():
    normalizer, responses = normalizer_answering(answer("girl in an orange sweater"))

    result = await normalizer.normalize("дівчина в помаранчевому светрі")

    assert result == NormalizedQuery(query="girl in an orange sweater", expansions=[])
    [call] = responses.calls
    assert call["model"] == "gpt-5-nano"
    assert call["instructions"] == INSTRUCTIONS
    # The query is the input on its own, never spliced into the prompt.
    assert call["input"] == "дівчина в помаранчевому светрі"


async def test_asks_for_the_query_and_its_expansions_as_structured_output():
    normalizer, responses = normalizer_answering(answer("resume", ["cv", "curriculum vitae"]))

    result = await normalizer.normalize("резюме")

    assert result == NormalizedQuery(query="resume", expansions=["cv", "curriculum vitae"])
    [call] = responses.calls
    output_format = call["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["required"] == ["query", "expansions"]


async def test_an_empty_expansion_list_is_fine():
    normalizer, _ = normalizer_answering(answer("IMG_2041.jpg", []))

    assert await normalizer.normalize("IMG_2041.jpg") == NormalizedQuery(query="IMG_2041.jpg", expansions=[])


@pytest.mark.parametrize(
    "query",
    ["Тарас Шевченко", "ACME Corp", "Zelenin_CV_2026.pdf", "INV-00042", "iPhone 15 Pro"],
)
async def test_names_and_identifiers_the_model_keeps_pass_through_exactly(query: str):
    """Not lowercased, transliterated, split or otherwise touched."""
    normalizer, _ = normalizer_answering(answer(query))

    assert await normalizer.normalize(query) == NormalizedQuery(query=query, expansions=[])


def test_the_prompt_keeps_proper_nouns_and_bounds_the_expansions():
    assert "Never translate or transliterate proper nouns" in INSTRUCTIONS
    assert "0 to 8" in INSTRUCTIONS
    assert "empty list" in INSTRUCTIONS


@pytest.mark.parametrize(
    "query",
    ['"girl"', "“girl”", "«girl»", "  girl\n", "'girl'"],
)
async def test_strips_quotes_and_whitespace_around_the_rewrite(query: str):
    normalizer, _ = normalizer_answering(answer(query))

    assert (await normalizer.normalize("дівчина")).query == "girl"


async def test_joins_a_multiline_rewrite_into_one_line():
    normalizer, _ = normalizer_answering(answer("book\nabout  magic"))

    assert (await normalizer.normalize("книга про магію")).query == "book about magic"


async def test_expansions_are_deduplicated_case_insensitively_and_never_repeat_the_query():
    normalizer, _ = normalizer_answering(
        answer("resume", ["CV", "cv", "Resume", " curriculum  vitae ", "Curriculum Vitae", "", "  ", "резюме"])
    )

    result = await normalizer.normalize("резюме")

    assert result.expansions == ["CV", "curriculum vitae"]


async def test_expansions_are_capped_whatever_the_model_returns():
    normalizer, _ = normalizer_answering(answer("receipt", [f"term {n}" for n in range(20)]))

    result = await normalizer.normalize("receipt")

    assert result.expansions == [f"term {n}" for n in range(MAX_EXPANSIONS)]


async def test_implausibly_long_expansions_are_dropped():
    normalizer, _ = normalizer_answering(answer("receipt", ["invoice", "a whole sentence about payments " * 3]))

    assert (await normalizer.normalize("receipt")).expansions == ["invoice"]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "receipt",  # Plain text, not JSON.
        '{"query": "receipt"',  # Cut off.
        '{"query": "receipt"}',  # No expansions.
        '{"expansions": ["invoice"]}',  # No query.
        '{"query": "receipt", "expansions": "invoice"}',
        '{"query": "receipt", "expansions": [1, 2]}',
        '{"query": null, "expansions": []}',
        '["receipt"]',
    ],
)
async def test_rejects_malformed_output(output: str):
    normalizer, _ = normalizer_answering(output)

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("receipt")


@pytest.mark.parametrize("query", ["", "   ", '""'])
async def test_rejects_an_empty_rewrite(query: str):
    normalizer, _ = normalizer_answering(answer(query, ["girl"]))

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("дівчина")


async def test_rejects_a_rewrite_far_longer_than_the_query():
    normalizer, _ = normalizer_answering(answer("Here are some ideas for what you might be looking for: " * 5))

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("cat")


async def test_rejects_an_incomplete_response():
    normalizer, _ = normalizer_answering('{"query": "girl in an', status="incomplete")

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("дівчина в помаранчевому светрі")


async def test_a_malformed_answer_is_not_quoted_in_the_error():
    normalizer, _ = normalizer_answering("my private medical letter")

    with pytest.raises(QueryNormalizationError) as raised:
        await normalizer.normalize("лист")

    assert "medical" not in str(raised.value)
    assert raised.value.__cause__ is None


async def test_errors_from_openai_propagate():
    class FailingResponses:
        async def create(self, **kwargs):
            raise ConnectionError("openai is unreachable")

    client = SimpleNamespace(responses=FailingResponses())
    normalizer = OpenAIQueryNormalizer(model="gpt-5-nano", timeout_seconds=5, client=client)

    with pytest.raises(ConnectionError):
        await normalizer.normalize("cat")


async def test_openai_keeps_no_stored_copy_and_nothing_logs_the_query(caplog):
    caplog.set_level("DEBUG")
    normalizer, responses = normalizer_answering(answer("hospital appointment letter", ["clinic visit"]))

    await normalizer.normalize("лист про прийом у лікарні")

    [call] = responses.calls
    assert call["store"] is False
    logged = " ".join(f"{record.getMessage()} {getattr(record, 'stash_fields', {})}" for record in caplog.records)
    assert "лікарні" not in logged
    assert "hospital" not in logged
    assert "clinic" not in logged
