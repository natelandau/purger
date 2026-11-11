"""Tests for the prek plugin."""

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import RiskLevel, RunCommand, SizeUnknown
from purger.plugins import prek
from purger.sizing import measure_plugin
from tests.conftest import make_completed


def test_prek_sizes_from_the_machine_readable_cache_size(mocker: MockerFixture) -> None:
    """Verify the total and the clean action read bytes from `prek cache size`."""
    # Given
    run = mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=make_completed(stdout="123456\n")
    )

    # When
    report = measure_plugin(
        prek.PLUGIN, include_dangerous=True, settings=Config().settings_for(prek.PLUGIN)
    )

    # Then
    assert report.total_size_bytes == 123456
    assert run.call_args.args[0] == list(prek.PREK_CACHE_SIZE_COMMAND)
    sizes = {a.action.name: a.size_bytes for a in report.actions}
    assert sizes == {"gc": None, "clean": 123456}


def test_prek_size_is_unknown_when_output_is_not_a_number(mocker: MockerFixture) -> None:
    """Verify unreadable size output leaves the total unknown instead of failing."""
    # Given
    mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=make_completed(stdout="n/a\n")
    )

    # When
    report = measure_plugin(
        prek.PLUGIN, include_dangerous=True, settings=Config().settings_for(prek.PLUGIN)
    )

    # Then
    assert report.total_size_bytes is None


@pytest.mark.parametrize(
    ("name", "argv", "risk"),
    [
        ("gc", ("prek", "cache", "gc"), RiskLevel.SAFE),
        ("clean", ("prek", "cache", "clean"), RiskLevel.DANGEROUS),
    ],
)
def test_prek_cleanups_run_without_sudo(name: str, argv: tuple[str, ...], risk: RiskLevel) -> None:
    """Verify each action runs its prek command as the user, at the expected risk."""
    # Given
    action = next(a for a in prek.PLUGIN.actions if a.name == name)

    # When / Then
    assert isinstance(action.cleanup, RunCommand)
    assert action.cleanup.argv == argv
    assert action.cleanup.with_sudo is False
    assert action.risk_level is risk


def test_prek_gc_size_is_unknown() -> None:
    """Verify gc reports an unknown size, since prek cannot preview it."""
    # Given
    action = next(a for a in prek.PLUGIN.actions if a.name == "gc")

    # When / Then
    assert isinstance(action.size, SizeUnknown)
