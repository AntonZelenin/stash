"""Secret settings: plain values locally, Secrets Manager ARNs on AWS."""

import json

from stash_shared import secrets

from app.config import Settings

DB_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/rds/master-AbCdEf"
OPENAI_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:stash-prod/openai/api-key-AbCdEf"


def test_plain_values_are_used_without_secret_arns(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/stash")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local")

    settings = Settings()

    assert (settings.database_url, settings.openai_api_key) == ("postgresql+asyncpg://u:p@db:5432/stash", "sk-local")


def test_secret_arns_replace_plain_values(monkeypatch):
    values = {
        DB_SECRET_ARN: json.dumps(
            {"host": "db.internal", "port": 5432, "dbname": "stash", "username": "stash", "password": "pw"}
        ),
        OPENAI_SECRET_ARN: "sk-from-secret",
    }
    monkeypatch.setattr(secrets, "get_secret_string", values.__getitem__)
    monkeypatch.setenv("DATABASE_SECRET_ARN", DB_SECRET_ARN)
    monkeypatch.setenv("OPENAI_API_KEY_SECRET_ARN", OPENAI_SECRET_ARN)

    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://stash:pw@db.internal:5432/stash"
    assert settings.openai_api_key == "sk-from-secret"
