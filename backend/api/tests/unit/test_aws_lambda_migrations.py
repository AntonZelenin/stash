"""The migration Lambda runs `alembic upgrade head` with the packaged config."""

from pathlib import Path

import pytest

from app import aws_lambda_migrations

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
