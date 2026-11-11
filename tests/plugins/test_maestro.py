"""Tests for the maestro plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import maestro


@pytest.mark.parametrize(
    ("xdg_state_home", "expected"),
    [
        (None, ("~/.maestro/tests/*", "~/.local/state/maestro/tests/*")),
        ("/Users/me/state/", ("~/.maestro/tests/*", "/Users/me/state/maestro/tests/*")),
    ],
)
def test_maestro_output_follows_xdg_state_home(
    xdg_state_home: str | None, expected: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify both the documented and the XDG test output folders are cleaned."""
    # Given
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if xdg_state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", xdg_state_home)

    # When / Then
    assert maestro._test_output_patterns() == expected


@pytest.mark.parametrize("has_output", [True, False], ids=["output", "nothing-to-clean"])
def test_maestro_available_only_with_output(
    has_output: bool, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the plugin applies only with run output to clean."""
    # Given
    if has_output:
        (tmp_path / ".maestro" / "tests" / "2026-11-10_041643").mkdir(parents=True)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    mocker.patch.object(maestro, "_TEST_OUTPUT_PATHS", ("~/.maestro/tests/*",))
    assert maestro.PLUGIN.is_available is not None

    # When / Then
    assert maestro.PLUGIN.is_available() is has_output


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/usr/bin/java", "-classpath", "/x/lib/*", "maestro.cli.AppKt", "test"], True),
        (["/bin/zsh"], False),
    ],
    ids=["maestro", "other"],
)
def test_maestro_busy_while_maestro_runs(cmdline: list[str], expected: bool) -> None:
    """Verify only a Maestro JVM process makes the plugin busy."""
    # Given
    assert maestro.PLUGIN.busy_process is not None

    # When / Then
    assert maestro.PLUGIN.busy_process(cmdline) is expected
