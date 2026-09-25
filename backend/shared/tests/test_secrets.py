import json
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from stash_shared import secrets

DB_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/rds/master-AbCdEf"
OPENAI_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/openai/api-key-AbCdEf"
DB_SECRET = {
    "engine": "postgres",
    "host": "stash-prod.abc123.eu-west-1.rds.amazonaws.com",
    "port": 5432,
    "dbname": "stash",
    "username": "stash",
    "password": "p@ss/word",
}


@pytest.fixture
def client(monkeypatch):
    """A real boto3 Secrets Manager client answered by a `Stubber`; never
    touches the network. The secret cache is cleared around each test."""
    secrets.get_secret_string.cache_clear()
    sm = boto3.client(
        "secretsmanager", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    monkeypatch.setattr(secrets, "_secrets_manager_client", lambda: sm)
    with Stubber(sm) as stubber:
        sm.stubber = stubber
        yield sm
        stubber.assert_no_pending_responses()
    secrets.get_secret_string.cache_clear()


def _expect(client, arn: str, value: str) -> None:
    client.stubber.add_response("get_secret_value", {"ARN": arn, "SecretString": value}, {"SecretId": arn})


def test_database_url_is_built_from_the_secret_with_credentials_escaped(client):
    _expect(client, DB_SECRET_ARN, json.dumps(DB_SECRET))

    assert secrets.database_url_from_secret(DB_SECRET_ARN) == (
        "postgresql+asyncpg://stash:p%40ss%2Fword@stash-prod.abc123.eu-west-1.rds.amazonaws.com:5432/stash"
    )


def test_resolve_replaces_plain_values_with_secret_ones(client):
    _expect(client, DB_SECRET_ARN, json.dumps(DB_SECRET))
    _expect(client, OPENAI_SECRET_ARN, "sk-test")
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://local",
        database_secret_arn=DB_SECRET_ARN,
        openai_api_key="",
        openai_api_key_secret_arn=OPENAI_SECRET_ARN,
    )

    secrets.resolve_secret_settings(settings)

    assert settings.database_url.startswith("postgresql+asyncpg://stash:")
    assert settings.openai_api_key == "sk-test"


def test_resolve_leaves_plain_values_alone_without_arns(client):
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://local", database_secret_arn="", openai_api_key="sk-local"
    )

    secrets.resolve_secret_settings(settings)

    assert (settings.database_url, settings.openai_api_key) == ("postgresql+asyncpg://local", "sk-local")


def test_resolve_database_url_leaves_the_openai_key_alone(client):
    _expect(client, DB_SECRET_ARN, json.dumps(DB_SECRET))
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://local",
        database_secret_arn=DB_SECRET_ARN,
        openai_api_key="",
        openai_api_key_secret_arn=OPENAI_SECRET_ARN,
    )

    # Only the database secret is stubbed: fetching the OpenAI one would raise.
    secrets.resolve_database_url(settings)

    assert settings.database_url.startswith("postgresql+asyncpg://stash:")
    assert settings.openai_api_key == ""


def test_openai_api_key_is_the_secret_value(client):
    _expect(client, OPENAI_SECRET_ARN, "sk-test")
    settings = SimpleNamespace(openai_api_key="", openai_api_key_secret_arn=OPENAI_SECRET_ARN)

    assert secrets.openai_api_key(settings) == "sk-test"


def test_openai_api_key_is_the_plain_value_without_an_arn():
    settings = SimpleNamespace(openai_api_key="sk-local", openai_api_key_secret_arn="")

    assert secrets.openai_api_key(settings) == "sk-local"


def test_an_unreadable_openai_secret_is_retried_later(client):
    # A secret created without a value yet.
    client.stubber.add_client_error(
        "get_secret_value", "ResourceNotFoundException", expected_params={"SecretId": OPENAI_SECRET_ARN}
    )
    _expect(client, OPENAI_SECRET_ARN, "sk-test")
    settings = SimpleNamespace(openai_api_key="", openai_api_key_secret_arn=OPENAI_SECRET_ARN)

    with pytest.raises(client.exceptions.ResourceNotFoundException):
        secrets.openai_api_key(settings)
    assert secrets.openai_api_key(settings) == "sk-test"


def test_each_secret_is_fetched_once(client):
    _expect(client, OPENAI_SECRET_ARN, "sk-test")

    assert secrets.get_secret_string(OPENAI_SECRET_ARN) == "sk-test"
    # A second fetch would find no stubbed response and raise.
    assert secrets.get_secret_string(OPENAI_SECRET_ARN) == "sk-test"


def test_the_client_ignores_the_dual_stack_setting(monkeypatch):
    monkeypatch.setenv("AWS_USE_DUALSTACK_ENDPOINT", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    endpoint = secrets._secrets_manager_client().meta.endpoint_url

    assert endpoint == "https://secretsmanager.eu-west-1.amazonaws.com"
