"""Secret settings from AWS Secrets Manager.

Locally, secrets are plain settings (`DATABASE_URL`, `OPENAI_API_KEY`). On
AWS the services get the ARNs of the secrets instead
(`DATABASE_SECRET_ARN`, `OPENAI_API_KEY_SECRET_ARN`), and each settings
class resolves them into those same fields when it's built
(`resolve_secret_settings`), so nothing downstream knows where a value came
from. Settings are built once per process (each service's cached
`get_settings`), and fetched secrets are cached here as well, so a Lambda
execution environment fetches each secret once, on its cold start.
"""

import json
from functools import lru_cache
from typing import Any
from urllib.parse import quote

# The scheme every service's DATABASE_URL uses (SQLAlchemy's async engine
# over asyncpg).
DATABASE_URL_SCHEME = "postgresql+asyncpg"


def resolve_secret_settings(settings: Any) -> None:
    """Fills `database_url` from `database_secret_arn`, and `openai_api_key`
    from `openai_api_key_secret_arn`, for whichever of those ARN fields
    `settings` has and sets. A set ARN wins over the plain value.

    `settings` is untyped, like in `stash_shared.queue.factory`: the API and
    each worker have their own settings class, and not every one has an
    OpenAI key."""
    resolve_database_url(settings)
    if getattr(settings, "openai_api_key_secret_arn", None):
        settings.openai_api_key = openai_api_key(settings)


def resolve_database_url(settings: Any) -> None:
    """Fills `database_url` from `database_secret_arn`, if `settings` sets
    it. For services that resolve the OpenAI key only when they need it
    (`openai_api_key`), rather than with everything else."""
    database_secret_arn = getattr(settings, "database_secret_arn", None)
    if database_secret_arn:
        settings.database_url = database_url_from_secret(database_secret_arn)


def openai_api_key(settings: Any) -> str:
    """The OpenAI key: the value of `openai_api_key_secret_arn` if set,
    else the plain `openai_api_key`. Raises if the secret can't be read
    (missing, no value yet, no access); only successful reads are cached,
    so a later call tries again."""
    secret_arn = getattr(settings, "openai_api_key_secret_arn", None)
    if secret_arn:
        return get_secret_string(secret_arn)
    return settings.openai_api_key


def database_url_from_secret(secret_arn: str) -> str:
    """The DATABASE_URL for a database secret: JSON with `host`, `port`,
    `dbname`, `username` and `password` (the shape Terraform writes for
    RDS)."""
    secret = json.loads(get_secret_string(secret_arn))
    username = quote(secret["username"], safe="")
    password = quote(secret["password"], safe="")
    return f"{DATABASE_URL_SCHEME}://{username}:{password}@{secret['host']}:{secret['port']}/{secret['dbname']}"


@lru_cache
def get_secret_string(secret_arn: str) -> str:
    """The secret's current value, fetched once per process."""
    response = _secrets_manager_client().get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def _secrets_manager_client():
    # Imported here: only AWS deployments read Secrets Manager.
    import boto3
    from botocore.config import Config

    # Always the standard endpoint, even where AWS_USE_DUALSTACK_ENDPOINT is
    # set for S3 and SQS: Secrets Manager's standard endpoint already serves
    # IPv6, and it has no `api.aws` dual-stack one to switch to.
    return boto3.client("secretsmanager", config=Config(use_dualstack_endpoint=False))
