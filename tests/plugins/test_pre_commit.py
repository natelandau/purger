"""Tests for the pre-commit plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import pre_commit


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"PRE_COMMIT_HOME": "/pch"}, Path("/pch")),
        ({"XDG_CACHE_HOME": "/xdg"}, Path("/xdg/pre-commit")),
        ({}, Path("/home/me/.cache/pre-commit")),
    ],
)
def test_pre_commit_cache_dir_lookup_order(
    env: dict[str, str], expected: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """Verify the cache directory follows pre-commit's own lookup order."""
    # Given
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mocker.patch("purger.plugins.pre_commit.invoking_home", return_value=Path("/home/me"))

    # When / Then
    assert pre_commit._cache_dir() == expected
