"""Tests for the pip plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import pip


def test_pip_available_only_with_a_cache(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the plugin applies only when a cache holds something, without pip installed."""
    # Given an empty home and no XDG_CACHE_HOME pointing outside it
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    mocker.patch.object(pip, "_CACHE_PATHS", pip._cache_patterns())
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    assert pip.PLUGIN.is_available is not None
    before = pip.PLUGIN.is_available()

    # When a Linux-style cache entry appears
    entry = tmp_path / ".cache" / "pip" / "entry"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"x")

    # Then
    assert before is False
    assert pip.PLUGIN.is_available() is True


def test_pip_cache_follows_xdg_cache_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the pip cache is found under XDG_CACHE_HOME, where pip writes on Linux."""
    # Given
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/me/xdg")

    # When
    patterns = pip._cache_patterns()

    # Then
    assert "/home/me/xdg/pip/*" in patterns
    assert "~/.cache/pip/*" in patterns


_PY = "/usr/bin/python3"


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ([_PY, "-m", "pip", "install", "requests"], True),
        (["/Users/me/.venv/bin/pip3.12", "install", "-r", "requirements.txt"], True),
        ([_PY, "/Users/me/.venv/bin/pip", "download", "x"], True),
        ([_PY, "-m", "pipx", "install", "x"], False),
    ],
)
def test_pip_busy_process_matches_pip_runs(cmdline: list[str], expected: bool) -> None:
    """Verify pip counts as busy however it was launched, and nothing else does."""
    # Given
    assert pip.PLUGIN.busy_process is not None

    # When / Then
    assert pip.PLUGIN.busy_process(cmdline) is expected
