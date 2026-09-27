"""The migration Lambda runs `alembic upgrade head` with the packaged config,
then provisions the runtime database roles."""

import base64
from pathlib import Path

import pytest

from app import aws_lambda_migrations, db_roles

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def upgrades(monkeypatch) -> list[tuple[str, str]]:
    calls = []
    monkeypatch.setattr(
        aws_lambda_migrations.command,
        "upgrade",
        lambda config, revision: calls.append((config.config_file_name, revision)),
    )
    monkeypatch.setenv("ALEMBIC_CONFIG", str(ALEMBIC_INI))
    return calls


def test_handler_upgrades_to_head(upgrades):
    aws_lambda_migrations.handler({}, None)

    assert upgrades == [(str(ALEMBIC_INI), "head")]


def test_handler_reports_the_head_revision(upgrades):
    result = aws_lambda_migrations.handler({}, None)

    assert result["status"] == "ok"
    assert next((ALEMBIC_INI.parent / "alembic" / "versions").glob(f"{result['revision']}_*.py"), None)


def test_handler_fails_when_the_migration_fails(monkeypatch):
    def fail(config, revision):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(aws_lambda_migrations.command, "upgrade", fail)
    monkeypatch.setenv("ALEMBIC_CONFIG", str(ALEMBIC_INI))

    with pytest.raises(RuntimeError, match="migration failed"):
        aws_lambda_migrations.handler({}, None)


def test_default_config_is_in_migrations_next_to_the_app_package():
    # In the Lambda package: migrations/alembic.ini, beside app/.
    code_root = Path(aws_lambda_migrations.__file__).resolve().parent.parent
    assert aws_lambda_migrations._DEFAULT_CONFIG == code_root / "migrations" / "alembic.ini"


def test_handler_provisions_the_runtime_roles_from_their_secrets(upgrades, monkeypatch):
    secrets = {
        "arn:api": '{"username": "stash_api", "password": "api-pw", "host": "db"}',
        "arn:worker": '{"username": "stash_worker", "password": "worker-pw", "host": "db"}',
    }
    provisioned = []

    async def provision_roles(database_url, *, api, worker):
        provisioned.append((database_url, api, worker))

    monkeypatch.setenv("DATABASE_API_SECRET_ARN", "arn:api")
    monkeypatch.setenv("DATABASE_WORKER_SECRET_ARN", "arn:worker")
    monkeypatch.setattr(aws_lambda_migrations, "get_secret_string", secrets.__getitem__)
    monkeypatch.setattr(aws_lambda_migrations, "provision_roles", provision_roles)

    result = aws_lambda_migrations.handler({}, None)

    # After the migration, as the migrating (owner) connection.
    assert upgrades and result["roles_provisioned"] is True
    [(database_url, api, worker)] = provisioned
    assert database_url == aws_lambda_migrations.get_settings().database_url
    assert api == db_roles.RoleLogin(name="stash_api", password="api-pw")
    assert worker == db_roles.RoleLogin(name="stash_worker", password="worker-pw")


def test_handler_leaves_roles_alone_without_their_secrets(upgrades, monkeypatch):
    monkeypatch.delenv("DATABASE_API_SECRET_ARN", raising=False)
    monkeypatch.setenv("DATABASE_WORKER_SECRET_ARN", "arn:worker")
    monkeypatch.setattr(aws_lambda_migrations, "provision_roles", pytest.fail)

    assert aws_lambda_migrations.handler({}, None)["roles_provisioned"] is False


def test_role_passwords_are_sent_as_scram_verifiers():
    # RFC 7677's test vector: user "user", password "pencil".
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    assert db_roles.scram_sha256_verifier("pencil", salt=salt) == (
        "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$"
        "WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY=:wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU="
    )


@pytest.mark.parametrize("name", ["", "Stash", 'a"; DROP ROLE x; --', "1role", "a" * 64])
def test_role_names_must_be_plain_identifiers(name):
    with pytest.raises(ValueError):
        db_roles._quote(name)
