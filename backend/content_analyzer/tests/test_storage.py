"""Empty S3 settings mean AWS S3 and the default credential chain (e.g. an
ECS task role), not literal empty strings."""

import boto3

from content_analyzer.storage import S3ObjectStore


def _client_kwargs(monkeypatch, **settings) -> dict:
    captured = {}
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: captured.update(kwargs))
    S3ObjectStore(bucket="stash", **settings)
    return captured


def test_empty_settings_use_aws_defaults(monkeypatch):
    kwargs = _client_kwargs(monkeypatch, endpoint_url="", access_key="", secret_key="")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (None, None, None)


def test_explicit_settings_are_passed_through(monkeypatch):
    kwargs = _client_kwargs(monkeypatch, endpoint_url="http://minio:9000", access_key="key", secret_key="secret")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (
        "http://minio:9000",
        "key",
        "secret",
    )
