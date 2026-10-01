import json
from types import SimpleNamespace

import openai
import pytest
from stash_worker_core.errors import PermanentProcessingError

from video_analyzer import describer as describer_module
from video_analyzer.describer import OpenAIVideoDescriber, parse_chunks
from video_analyzer.frames import Frame


def _answer(summary="A dog plays fetch on a beach.", chunks=(), visible_text=()) -> str:
    return json.dumps({"summary": summary, "chunks": list(chunks), "visible_text": list(visible_text)})


def test_summary_first_then_chunks_then_text():
    output = _answer(
        chunks=["golden retriever, dog, pet", "dog catching a frisbee", "sandy beach, ocean shoreline"],
        visible_text=["SURF SHOP", "Пляж"],
    )

    assert parse_chunks(output) == [
        "A dog plays fetch on a beach.",
        "golden retriever, dog, pet",
        "dog catching a frisbee",
        "sandy beach, ocean shoreline",
        "text: SURF SHOP",
        "text: Пляж",
    ]


def test_chunks_are_one_line_each_without_blanks_or_repeats():
    output = _answer(
        summary="  A  dog\nplays. ",
        chunks=["dog\n running", "", "  ", "DOG RUNNING", 7, "beach"],
        visible_text=["beach", "EXIT\n 3"],
    )

    assert parse_chunks(output) == ["A dog plays.", "dog running", "beach", "text: EXIT 3"]


def test_runaway_answers_are_capped():
    output = _answer(chunks=[f"chunk {i}" for i in range(50)], visible_text=[f"sign {i}" for i in range(50)])

    chunks = parse_chunks(output)

    assert len(chunks) == 1 + 20 + 15


def test_an_empty_summary_is_left_out():
    assert parse_chunks(_answer(summary="", chunks=["beach"])) == ["beach"]


@pytest.mark.parametrize("output", ["not json", "[]", json.dumps({"summary": "x"}), json.dumps({"chunks": "x"})])
def test_unexpected_answers_raise_as_transient(output):
    with pytest.raises(RuntimeError):
        parse_chunks(output)


class _FakeResponses:
    def __init__(self, output_text: str = "", error: Exception | None = None):
        self.output_text = output_text
        self.error = error
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status="completed", output_text=self.output_text)


def _describer(monkeypatch, responses: _FakeResponses) -> OpenAIVideoDescriber:
    monkeypatch.setattr(
        describer_module,
        "build_openai_client",
        lambda **kwargs: SimpleNamespace(responses=responses),
    )
    return OpenAIVideoDescriber(api_key="sk-test", model="test-model", timeout_seconds=1)


_FRAMES = [Frame(time_seconds=3.0, jpeg=b"\xff\xd8one"), Frame(time_seconds=75.5, jpeg=b"\xff\xd8two")]


async def test_sends_every_frame_in_order_in_one_request_labelled_with_its_time(monkeypatch):
    responses = _FakeResponses(_answer(chunks=["beach"]))

    chunks = await _describer(monkeypatch, responses).describe(_FRAMES, duration_seconds=90)

    assert chunks == ["A dog plays fetch on a beach.", "beach"]
    [request] = responses.requests
    assert request["model"] == "test-model"
    # The video is the user's: nothing kept by OpenAI to read back.
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert "chronological" in request["instructions"]
    [message] = request["input"]
    content = message["content"]
    assert content[0] == {"type": "input_text", "text": "The video is 1:30 long. 2 frames, in order:"}
    assert content[1] == {"type": "input_text", "text": "Frame 1 at 0:03:"}
    assert content[2]["type"] == "input_image" and content[2]["image_url"].startswith("data:image/jpeg;base64,")
    assert content[3] == {"type": "input_text", "text": "Frame 2 at 1:15:"}
    assert [part["type"] for part in content].count("input_image") == 2


def test_people_are_named_by_what_is_apparent_never_by_who_they_are():
    """Asked only not to guess identities, the model wrote "several adults"
    for a group of women: gender and age that are plain to see are search
    terms, names are not."""
    instructions = describer_module._INSTRUCTIONS

    assert "name people concretely by what is visibly apparent (woman, man" in instructions
    assert "never say who someone is" in instructions


async def test_unknown_duration_is_said_so(monkeypatch):
    responses = _FakeResponses(_answer(chunks=["beach"]))

    await _describer(monkeypatch, responses).describe(_FRAMES[:1], duration_seconds=None)

    assert responses.requests[0]["input"][0]["content"][0]["text"] == "Its length is unknown. 1 frames, in order:"


async def test_rejected_input_is_permanent(monkeypatch):
    error = openai.BadRequestError(
        "bad image", response=SimpleNamespace(status_code=400, request=None, headers={}), body=None
    )
    describer = _describer(monkeypatch, _FakeResponses(error=error))

    with pytest.raises(PermanentProcessingError):
        await describer.describe(_FRAMES, duration_seconds=90)


async def test_an_answer_without_chunks_is_transient(monkeypatch):
    describer = _describer(monkeypatch, _FakeResponses(_answer(summary="", chunks=[])))

    with pytest.raises(RuntimeError):
        await describer.describe(_FRAMES, duration_seconds=90)
