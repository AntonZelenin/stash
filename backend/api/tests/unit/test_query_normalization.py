from types import SimpleNamespace

import pytest

from app.query_normalization import INSTRUCTIONS, OpenAIQueryNormalizer, QueryNormalizationError


class FakeResponses:
    """`client.responses`: answers every call with `output_text`."""

    def __init__(self, output_text: str, status: str = "completed"):
        self.output_text = output_text
        self.status = status
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status, output_text=self.output_text)


def normalizer_answering(output_text: str, status: str = "completed") -> tuple[OpenAIQueryNormalizer, FakeResponses]:
    responses = FakeResponses(output_text, status)
    client = SimpleNamespace(responses=responses)
    return OpenAIQueryNormalizer(model="gpt-5-nano", timeout_seconds=5, client=client), responses


async def test_sends_the_query_as_input_with_the_rewrite_instructions():
    normalizer, responses = normalizer_answering("girl in an orange sweater")

    result = await normalizer.normalize("дівчина в помаранчевому светрі")

    assert result == "girl in an orange sweater"
    [call] = responses.calls
    assert call["model"] == "gpt-5-nano"
    assert call["instructions"] == INSTRUCTIONS
    # The query is the input on its own, never spliced into the prompt.
    assert call["input"] == "дівчина в помаранчевому светрі"


@pytest.mark.parametrize(
    "output",
    ['"girl"', "“girl”", "«girl»", "  girl\n", "'girl'"],
)
async def test_strips_quotes_and_whitespace_around_the_rewrite(output: str):
    normalizer, _ = normalizer_answering(output)

    assert await normalizer.normalize("дівчина") == "girl"


async def test_joins_a_multiline_rewrite_into_one_line():
    normalizer, _ = normalizer_answering("book\nabout  magic")

    assert await normalizer.normalize("книга про магію") == "book about magic"


@pytest.mark.parametrize("output", ["", "   ", '""'])
async def test_rejects_an_empty_rewrite(output: str):
    normalizer, _ = normalizer_answering(output)

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("дівчина")


async def test_rejects_a_rewrite_far_longer_than_the_query():
    normalizer, _ = normalizer_answering("Here are some ideas for what you might be looking for: " * 5)

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("cat")


async def test_rejects_an_incomplete_response():
    normalizer, _ = normalizer_answering("girl in an", status="incomplete")

    with pytest.raises(QueryNormalizationError):
        await normalizer.normalize("дівчина в помаранчевому светрі")


async def test_errors_from_openai_propagate():
    class FailingResponses:
        async def create(self, **kwargs):
            raise ConnectionError("openai is unreachable")

    client = SimpleNamespace(responses=FailingResponses())
    normalizer = OpenAIQueryNormalizer(model="gpt-5-nano", timeout_seconds=5, client=client)

    with pytest.raises(ConnectionError):
        await normalizer.normalize("cat")
