import pytest

from stash_shared.redaction import (
    REDACTED,
    is_sensitive_name,
    redact_fields,
    redact_span_attributes,
    redact_text,
    redact_url,
)

PRESIGNED = (
    "https://stash.s3.eu-west-1.amazonaws.com/uploads/u/1?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAEXAMPLE%2F20260927%2Feu-west-1%2Fs3%2Faws4_request&X-Amz-Date=20260927T000000Z"
    "&X-Amz-Expires=900&X-Amz-Security-Token=FQoGZXIvYXdzEXAMPLE&X-Amz-Signature=deadbeef"
)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "http.request.header.authorization",
        "Cookie",
        "Set-Cookie",
        "access_token",
        "refresh_token",
        "token",
        "turnstile_token",
        "cf-turnstile-response",
        "password",
        "current_password",
        "password_hash",
        "secret",
        "turnstile_secret_key",
        "api_key",
        "openai_api_key",
        "X-Amz-Security-Token",
        "X-Amz-Signature",
        "X-Amz-Credential",
    ],
)
def test_sensitive_names(name):
    assert is_sensitive_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "item_id",
        "user_id",
        "storage_key",
        "staging_key",
        "max_output_tokens",
        "input_tokens",
        "session_id",
        "content_type",
        "passed_threshold",
        "error_codes",
        "hostname",
    ],
)
def test_metadata_names_are_kept(name):
    assert not is_sensitive_name(name)


def test_sensitive_fields_are_replaced_whole_nested_too():
    fields = {
        "Authorization": "Bearer abc",
        "headers": {"cookie": "refresh_token=abc", "accept": "application/json"},
        "password": "hunter2",
        "item_id": "1",
        "size_bytes": 10,
        "skipped": None,
    }

    assert redact_fields(fields) == {
        "Authorization": REDACTED,
        "headers": {"cookie": REDACTED, "accept": "application/json"},
        "password": REDACTED,
        "item_id": "1",
        "size_bytes": 10,
        "skipped": None,
    }


def test_presigned_url_loses_its_query_string():
    redacted = redact_text(f"PUT {PRESIGNED} failed")

    assert redacted == f"PUT https://stash.s3.eu-west-1.amazonaws.com/uploads/u/1?{REDACTED} failed"
    for secret in ("AKIAEXAMPLE", "FQoGZXIvYXdzEXAMPLE", "deadbeef"):
        assert secret not in redacted


def test_redact_url_keeps_only_scheme_host_and_path():
    assert redact_url(PRESIGNED) == "https://stash.s3.eu-west-1.amazonaws.com/uploads/u/1"
    assert redact_url("https://user:pw@collector/v1?key=1#x") == f"https://user:{REDACTED}@collector/v1"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("Cookie: refresh_token=r3fr3sh; other=value", "r3fr3sh"),
        ("Set-Cookie: refresh_token=r3fr3sh; HttpOnly; Secure", "r3fr3sh"),
        ("{'email': 'a@b.c', 'password': 'hunter2'}", "hunter2"),
        ('{"refresh_token": "r3fr3sh"}', "r3fr3sh"),
        ("turnstile_token=0.abcDEF", "0.abcDEF"),
        ("password=hunter2&next=/", "hunter2"),
        ("Incorrect API key provided: sk-proj-0123456789abcdef", "0123456789abcdef"),
        ("could not connect to postgresql+asyncpg://stash:s3cr3t@db:5432/stash", "s3cr3t"),
        ("ALTER ROLE stash_api WITH LOGIN PASSWORD 'SCRAM-SHA-256$4096:c2FsdA==$a2V5:c2Vydg=='", "SCRAM-SHA-256"),
    ],
)
def test_secrets_in_free_text_are_redacted(text, secret):
    redacted = redact_text(text)

    assert secret not in redacted
    assert REDACTED in redacted


def test_sqlalchemy_bound_parameters_are_redacted():
    error = (
        "(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) duplicate key\n"
        "[SQL: INSERT INTO item_texts (item_id, text) VALUES ($1::UUID, $2::VARCHAR)]\n"
        "[parameters: ('0000-1', 'my private note')]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )

    redacted = redact_text(error)

    assert "my private note" not in redacted
    assert "[SQL: INSERT INTO item_texts (item_id, text) VALUES ($1::UUID, $2::VARCHAR)]" in redacted
    assert "https://sqlalche.me/e/20/gkpj" in redacted


def test_metadata_in_free_text_is_kept():
    text = "Job attempt failed: storage_key=users/1/images/2/abc.png max_output_tokens=256 status_code=429"

    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        f"PUT {PRESIGNED}",
        "Authorization: Bearer abc.def",
        "Cookie: a=b; c=d",
        "{'password': 'x', 'token': \"y\"}",
        "postgresql://u:p@h/db",
    ],
)
def test_redaction_is_idempotent(text):
    once = redact_text(text)

    assert redact_text(once) == once


def test_span_url_attributes_lose_their_query_string():
    attributes = {
        "http.target": "/tags?query=holiday+in+kyiv",
        "http.url": "https://api.example.com/tags?query=holiday+in+kyiv",
        "url.full": "https://api.example.com/tags?query=holiday",
        "url.query": "query=holiday",
        "url.path": "/tags",
        "http.request.header.authorization": ("Bearer abc",),
        "http.status_code": 200,
    }

    assert redact_span_attributes(attributes) == {
        "http.target": f"/tags?{REDACTED}",
        "http.url": f"https://api.example.com/tags?{REDACTED}",
        "url.full": f"https://api.example.com/tags?{REDACTED}",
        "url.query": REDACTED,
        "url.path": "/tags",
        "http.request.header.authorization": REDACTED,
        "http.status_code": 200,
    }
