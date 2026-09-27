"""Processing limits are settings: the same ones locally (docker-compose)
as in production, overridable by environment, defaulting to the parsers'."""

import dataclasses
import pathlib
import re

import pytest

from document_analyzer.config import Settings
from document_analyzer.parsers import ParserLimits
from document_analyzer.stage import parser_limits

_COMPOSE = pathlib.Path(__file__).parents[4] / "docker-compose.yml"
_LIMITS = [name for name in Settings.model_fields if name.startswith(("document_max_", "document_parse_"))]


def _compose_environment(service: str) -> set[str]:
    """The variable names in `service`'s environment in docker-compose.yml."""
    compose = _COMPOSE.read_text(encoding="utf-8")
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \S|\Z)", compose, re.MULTILINE | re.DOTALL).group(1)
    return set(re.findall(r"^      ([A-Z][A-Z0-9_]*):", block, re.MULTILINE))


@pytest.mark.skipif(not _COMPOSE.exists(), reason="outside the repository")
def test_every_processing_limit_is_passed_on_by_docker_compose():
    missing = {name.upper() for name in _LIMITS} - _compose_environment("document_analyzer")

    assert not missing


def test_processing_limits_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_PDF_PAGE_CONTENT_BYTES", "65536")
    monkeypatch.setenv("DOCUMENT_MAX_TEXT_CHARS", "5000")
    monkeypatch.setenv("DOCUMENT_PARSE_TIMEOUT_SECONDS", "7.5")

    limits = parser_limits(Settings())

    assert (limits.max_pdf_page_content_bytes, limits.max_text_chars, limits.timeout_seconds) == (65536, 5000, 7.5)


def test_unset_processing_limits_are_the_production_defaults(monkeypatch):
    for name in _LIMITS:
        monkeypatch.delenv(name.upper(), raising=False)

    limits = parser_limits(Settings(_env_file=None))

    # ParserLimits' defaults are the settings' (excerpt_chars follows the
    # describer's budget).
    assert dataclasses.replace(limits, excerpt_chars=ParserLimits().excerpt_chars) == ParserLimits()
    assert (limits.max_text_chars, limits.max_pdf_page_content_bytes, limits.max_download_bytes) == (
        240_000, 2 * 1024 * 1024, 512 * 1024 * 1024
    )
