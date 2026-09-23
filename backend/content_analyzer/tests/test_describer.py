import openai
import pytest

try:  # the transport library the installed openai SDK builds its errors on
    import httpx2 as httpx
except ImportError:
    import httpx

from content_analyzer.describer import OpenAIImageDescriber
from content_analyzer.errors import PermanentProcessingError


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
