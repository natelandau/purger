"""Tests for the corepack plugin."""

import pytest

from purger.plugins import corepack


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "~/.cache/node/corepack"),
        ({"XDG_CACHE_HOME": "/xdg"}, "/xdg/node/corepack"),
        ({"XDG_CACHE_HOME": "/xdg", "COREPACK_HOME": "/cp"}, "/cp"),
    ],
)
def test_corepack_home(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str) -> None:
    """Verify COREPACK_HOME wins, then XDG_CACHE_HOME, on every platform."""
    # Given
    monkeypatch.delenv("COREPACK_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert corepack._home() == expected
