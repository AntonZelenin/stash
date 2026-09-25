"""Secret settings: plain values locally, Secrets Manager ARNs on AWS."""

import json

import pytest
from stash_shared import secrets

from app import config
from app.config import Settings

DB_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/rds/master-AbCdEf"
OPENAI_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/openai/api-key-AbCdEf"


def test_plain_values_are_used_without_secret_arns(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/stash")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local")

    settings = Settings()

    assert (settings.database_url, settings.openai_api_key) == ("postgresql+asyncpg://u:p@db:5432/stash", "sk-local")


class FakeSecretsManager:
    """Secret values by ARN, and which were fetched. A secret without a
    value (like one Terraform created and nobody filled yet) can't be read."""

    def __init__(self):
        self.values = {
            DB_SECRET_ARN: json.dumps(
                {"host": "db.internal", "port": 5432, "dbname": "stash", "username": "stash", "password": "pw"}
            ),
        }
        self.fetched: list[str] = []

    def get_secret_string(self, arn: str) -> str:
        self.fetched.append(arn)
        if arn not in self.values:
            raise LookupError(f"no value for {arn}")
        return self.values[arn]


@pytest.fixture
def secrets_manager(monkeypatch) -> FakeSecretsManager:
    fake = FakeSecretsManager()
    monkeypatch.setattr(secrets, "get_secret_string", fake.get_secret_string)
    monkeypatch.setenv("DATABASE_SECRET_ARN", DB_SECRET_ARN)
    monkeypatch.setenv("OPENAI_API_KEY_SECRET_ARN", OPENAI_SECRET_ARN)
    return fake


def test_the_database_secret_is_resolved_at_startup(secrets_manager):
    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://stash:pw@db.internal:5432/stash"


def test_the_openai_secret_is_not_read_at_startup(secrets_manager):
    # Its secret has no value: startup must not need it.
    settings = Settings()

    assert secrets_manager.fetched == [DB_SECRET_ARN]
    assert settings.openai_api_key == ""


def test_the_openai_key_comes_from_its_secret_when_needed(secrets_manager, monkeypatch):
    secrets_manager.values[OPENAI_SECRET_ARN] = "sk-from-secret"
    monkeypatch.setattr(config, "get_settings", Settings)

    assert config.get_openai_api_key() == "sk-from-secret"


def test_an_unreadable_openai_secret_fails_only_when_needed(secrets_manager, monkeypatch):
    monkeypatch.setattr(config, "get_settings", Settings)

    with pytest.raises(LookupError):
        config.get_openai_api_key()
