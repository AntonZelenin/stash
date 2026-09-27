"""Processing limits are settings: the same ones locally (docker-compose)
as in production, overridable by environment, defaulting to the handler's."""

import dataclasses
import pathlib
import re

import pytest

from thumbnailer.config import Settings
from thumbnailer.handler import ImageLimits

_COMPOSE = pathlib.Path(__file__).parents[4] / "docker-compose.yml"
# Not THUMBNAIL_MAX_SIZE: that's the thumbnail's size, not a limit.
_LIMITS = [f"thumbnail_{field.name}" for field in dataclasses.fields(ImageLimits)] + ["thumbnail_max_source_bytes"]


def _compose_environment(service: str) -> set[str]:
    """The variable names in `service`'s environment in docker-compose.yml."""
    compose = _COMPOSE.read_text(encoding="utf-8")
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \S|\Z)", compose, re.MULTILINE | re.DOTALL).group(1)
    return set(re.findall(r"^      ([A-Z][A-Z0-9_]*):", block, re.MULTILINE))


@pytest.mark.skipif(not _COMPOSE.exists(), reason="outside the repository")
def test_every_processing_limit_is_passed_on_by_docker_compose():
    missing = {name.upper() for name in _LIMITS} - _compose_environment("thumbnailer")

    assert not missing


def test_processing_limits_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("THUMBNAIL_MAX_PIXELS", "1000000")
    monkeypatch.setenv("THUMBNAIL_MAX_WIDTH", "4000")

    settings = Settings()

    assert (settings.thumbnail_max_pixels, settings.thumbnail_max_width) == (1_000_000, 4000)


def test_unset_processing_limits_are_the_production_defaults(monkeypatch):
    for name in _LIMITS:
        monkeypatch.delenv(name.upper(), raising=False)

    settings = Settings(_env_file=None)

    assert ImageLimits(
        max_width=settings.thumbnail_max_width,
        max_height=settings.thumbnail_max_height,
        max_declared_pixels=settings.thumbnail_max_declared_pixels,
        max_pixels=settings.thumbnail_max_pixels,
    ) == ImageLimits() == ImageLimits(50_000, 50_000, 250_000_000, 50_000_000)
    assert settings.thumbnail_max_source_bytes == 100 * 1024 * 1024
