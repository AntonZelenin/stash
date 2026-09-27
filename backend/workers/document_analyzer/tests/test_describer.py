from types import SimpleNamespace

from document_analyzer.describer import OpenAIDocumentDescriber

_TEXT = "Patient: Olena Kovalenko. Diagnosis: ..."


def _describer_answering(monkeypatch, output_text: str, requests: list) -> OpenAIDocumentDescriber:
    describer = OpenAIDocumentDescriber(api_key="test", model="test-model", timeout_seconds=1)

    async def _create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(status="completed", output_text=output_text)

    monkeypatch.setattr(describer._client.responses, "create", _create)
    return describer


async def test_openai_keeps_no_stored_copy_and_nothing_logs_the_document_or_answer(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    requests = []
    describer = _describer_answering(monkeypatch, "A medical report about Olena Kovalenko.", requests)

    description = await describer.describe(filename="kovalenko-report.pdf", text=_TEXT, is_partial=False)

    assert description == "A medical report about Olena Kovalenko."
    [request] = requests
    assert request["store"] is False
    assert _TEXT in request["input"]
    logged = " ".join(f"{record.getMessage()} {getattr(record, 'stash_fields', {})}" for record in caplog.records)
    assert "Kovalenko" not in logged
    assert "kovalenko-report" not in logged
    [call] = [record for record in caplog.records if record.getMessage() == "External call succeeded"]
    assert (call.stash_fields["input_chars"], call.stash_fields["output_chars"]) == (len(_TEXT), len(description))
