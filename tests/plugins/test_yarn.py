"""Tests for the yarn plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import yarn


def test_yarn_available_only_with_a_cache(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the plugin applies only when a cache holds something, without Yarn installed."""
    # Given an empty home and no XDG_CACHE_HOME pointing outside it
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    mocker.patch.object(yarn, "YARN_CACHE_GLOBS", yarn.yarn_cache_globs())
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    assert yarn.PLUGIN.skip_unless is not None
    before = yarn.PLUGIN.skip_unless()

    # When a Linux-style cache entry appears
    entry = tmp_path / ".cache" / "yarn" / "entry"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"x")

    # Then
    assert before is False
    assert yarn.PLUGIN.skip_unless() is True


def test_yarn_cache_follows_xdg_cache_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the Yarn 1 cache is found under XDG_CACHE_HOME, where Yarn writes on Linux."""
    # Given
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/me/xdg")

    # When
    patterns = yarn.yarn_cache_globs()

    # Then
    assert "/home/me/xdg/yarn/*" in patterns
    assert "~/.cache/yarn/*" in patterns


_NODE = "/opt/homebrew/bin/node"


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ([_NODE, "/opt/homebrew/bin/yarn", "install"], True),
        ([_NODE, "/repo/.yarn/releases/yarn-4.5.0.cjs", "install"], True),
        ([_NODE, "/repo/yarn-audit.js"], False),
        ([_NODE, "/opt/homebrew/bin/yarn", "run", "dev"], False),
    ],
)
def test_yarn_skip_while_running_matches_yarn_runs(cmdline: list[str], expected: bool) -> None:
    """Verify Yarn counts as running however it was launched, and nothing else does."""
    # Given
    assert yarn.PLUGIN.skip_while_running is not None

    # When / Then
    assert yarn.PLUGIN.skip_while_running(cmdline) is expected
