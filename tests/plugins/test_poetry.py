"""Tests for the poetry plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import poetry


def test_poetry_available_with_cache_or_virtualenvs(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Poetry applies when it left a cache or a virtualenv, without poetry installed."""
    # Given an empty home and no environment overrides pointing outside it
    for var in ("XDG_CACHE_HOME", "POETRY_CACHE_DIR", "POETRY_VIRTUALENVS_PATH"):
        monkeypatch.delenv(var, raising=False)
    mocker.patch.object(poetry, "_CACHE_PATHS", poetry._cache_patterns())
    mocker.patch.object(poetry, "_VIRTUALENV_PATHS", poetry._virtualenv_patterns())
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    assert poetry.PLUGIN.is_available is not None
    before = poetry.PLUGIN.is_available()

    # When a Linux-style virtualenv appears
    venv = tmp_path / ".cache" / "pypoetry" / "virtualenvs" / "app-abc123-py3.13"
    venv.mkdir(parents=True)

    # Then
    assert before is False
    assert poetry.PLUGIN.is_available() is True


def test_poetry_cache_paths_follow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify POETRY_CACHE_DIR and XDG_CACHE_HOME add cache folders without dropping the defaults."""
    # Given
    monkeypatch.setenv("POETRY_CACHE_DIR", "/data/poetry")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/me/xdg")

    # When
    patterns = poetry._cache_patterns()

    # Then
    assert "/data/poetry/cache/*" in patterns
    assert "/data/poetry/artifacts/*" in patterns
    assert "/home/me/xdg/pypoetry/artifacts/*" in patterns
    assert "~/Library/Caches/pypoetry/cache/*" in patterns
    assert "~/.cache/pypoetry/artifacts/*" in patterns
    assert not any("virtualenvs" in pattern for pattern in patterns)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"POETRY_VIRTUALENVS_PATH": "/venvs"}, "/venvs/*"),
        ({"POETRY_CACHE_DIR": "/data/poetry"}, "/data/poetry/virtualenvs/*"),
        ({}, "~/Library/Caches/pypoetry/virtualenvs/*"),
    ],
)
def test_poetry_virtualenv_paths_follow_environment(
    env: dict[str, str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the virtualenv folder follows Poetry's overrides, first in the list, and keeps the defaults."""
    # Given
    for var in ("XDG_CACHE_HOME", "POETRY_CACHE_DIR", "POETRY_VIRTUALENVS_PATH"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # When
    patterns = poetry._virtualenv_patterns()

    # Then
    assert patterns[0] == expected
    assert "~/.cache/pypoetry/virtualenvs/*" in patterns
    assert len(patterns) == len(set(patterns))
