"""Tests for the rip2 plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import rip2
from purger.sizing import disk_usage, measure_plugin
from tests.conftest import make_completed

_RIP2_HELP = (
    "Subcommands:\n"
    "  completions  Generate shell completions file\n"
    "  graveyard    Print the graveyard path\n"
)
_OLD_RIP_HELP = "rip: a safe and ergonomic alternative to rm\n    -d, --decompose\n"


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [(0, _RIP2_HELP, True), (0, _OLD_RIP_HELP, False), (1, _RIP2_HELP, False)],
    ids=["rip2", "old-rip", "help-fails"],
)
def test_rip2_available_only_for_rip2(
    returncode: int, stdout: str, expected: bool, mocker: MockerFixture
) -> None:
    """Verify the plugin applies only when `rip` is rip2, since the original rip would bury a file named graveyard."""
    # Given
    mocker.patch(
        "purger.plugins.rip2.run_command",
        autospec=True,
        return_value=make_completed(returncode=returncode, stdout=stdout),
    )
    assert rip2.PLUGIN.is_available is not None

    # When / Then
    assert rip2.PLUGIN.is_available() is expected


def test_rip2_measures_printed_graveyard(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the footprint is the size of the directory `rip graveyard` prints."""
    # Given
    (tmp_path / "buried").write_bytes(b"x" * 10_000)
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        return_value=make_completed(stdout=f"{tmp_path}\n"),
    )

    # When
    report = measure_plugin(rip2.PLUGIN, include_dangerous=False)

    # Then
    assert report.footprint_bytes == disk_usage([tmp_path])
