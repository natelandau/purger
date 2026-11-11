"""Tests for the cocoapods plugin."""

import pytest

from purger.plugins import cocoapods


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "~/Library/Caches/CocoaPods"),
        ({"CP_HOME_DIR": "/pods"}, "/pods/cache"),
        ({"CP_HOME_DIR": "/pods", "CP_CACHE_DIR": "/cache"}, "/cache"),
        ({"CP_CACHE_DIR": "relative"}, "~/Library/Caches/CocoaPods"),
        ({"CP_HOME_DIR": "relative"}, "~/Library/Caches/CocoaPods"),
    ],
)
def test_cocoapods_cache_dir(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Verify CP_CACHE_DIR wins over CP_HOME_DIR, which wins over the default."""
    # Given
    monkeypatch.delenv("CP_HOME_DIR", raising=False)
    monkeypatch.delenv("CP_CACHE_DIR", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # When / Then
    assert cocoapods._cache_dir() == expected
