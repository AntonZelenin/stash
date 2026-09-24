"""Empty S3 settings mean AWS S3 and the default credential chain (e.g. an
ECS task role), not literal empty strings."""

import boto3

from app.storage.minio import MinioStorage


def _clients(monkeypatch, **settings) -> list[dict]:
    created = []

    def fake_client(service, **kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    MinioStorage(bucket="stash", **settings)
    return created


def test_empty_settings_use_aws_defaults_with_one_client(monkeypatch):
    [kwargs] = _clients(monkeypatch, endpoint_url="", public_endpoint_url="", access_key="", secret_key="")

    assert (kwargs["endpoint_url"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]) == (None, None, None)


def test_separate_public_endpoint_gets_its_own_client(monkeypatch):
    internal, public = _clients(
        monkeypatch,
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
    )

    assert (internal["endpoint_url"], public["endpoint_url"]) == ("http://minio:9000", "http://localhost:9000")
    assert public["aws_access_key_id"] == "key" and public["aws_secret_access_key"] == "secret"
