"""Tests for the brew plugin."""

from pathlib import Path

import pytest
from nclutils.sh import ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugins import brew
from purger.plugins.brew import bytes_freed_by_cleanup
from purger.sizing import disk_usage, measure_plugin
from tests.conftest import make_completed


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Would remove: x (64B)\n==> This operation would free approximately 3GB of disk space.",
            3 * 1024**3,
        ),
        ("==> This operation would free approximately 861.6KB of disk space.", 882_278),
        ("==> This operation would free approximately 12MB of disk space.", 12 * 1024**2),
        ("==> This operation would free approximately 64B of disk space.", 64),
        ("Would remove: x (64B)", 0),
        ("", 0),
    ],
)
def test_parse_dry_run_extracts_freed_bytes(output: str, expected: int) -> None:
    """Verify the brew parser reads the freed-space summary line in binary units."""
    # Given / When
    result = bytes_freed_by_cleanup(output)

    # Then
    assert result == expected


def test_brew_autoremove_names_strip_header_and_tap() -> None:
    """Verify the autoremove dry run yields Cellar names, without the header or tap prefix."""
    # Given
    output = "==> Would autoremove 2 unneeded formulae:\nlibfoo\nuser/tap/bar\n"

    # When / Then
    assert brew._autoremove_names(output) == ["libfoo", "bar"]


def test_brew_autoremove_size_measures_cellar_racks(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the autoremove size sums only the racks of formulae it would remove."""
    # Given a Cellar with one removable and one kept formula
    (tmp_path / "libfoo" / "1.0").mkdir(parents=True)
    (tmp_path / "libfoo" / "1.0" / "lib").write_bytes(b"x" * 10_000)
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept" / "big").write_bytes(b"x" * 50_000)
    outputs = {
        ("brew", "--cellar"): f"{tmp_path}\n",
        ("brew", "autoremove", "--dry-run"): "==> Would autoremove 1 unneeded formula:\nlibfoo\n",
    }
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: make_completed(stdout=outputs[tuple(argv)]),
    )

    # When
    size = brew.bytes_freed_by_autoremove()

    # Then
    assert size == disk_usage([tmp_path / "libfoo"])


def test_brew_autoremove_size_zero_when_nothing_removable(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify an empty dry run measures zero, since brew prints nothing then."""
    # Given
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: make_completed(
            stdout=f"{tmp_path}\n" if "--cellar" in argv else ""
        ),
    )

    # When / Then
    assert brew.bytes_freed_by_autoremove() == 0


def test_brew_total_size_counts_versions_both_actions_remove_once(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify an old version that cleanup and autoremove both remove is counted once."""
    # Given a removable formula whose old version cleanup also removes
    old = tmp_path / "libfoo" / "1.0"
    current = tmp_path / "libfoo" / "2.0"
    for version in (old, current):
        version.mkdir(parents=True)
        (version / "lib").write_bytes(b"x" * 10_000)
    cleanup_output = f"Would remove: {old} (1 file, 1KB)\n==> This operation would free approximately 1KB of disk space.\n"
    outputs = {
        ("brew", "--cellar"): f"{tmp_path}\n",
        ("brew", "autoremove", "--dry-run"): "==> Would autoremove 1 unneeded formula:\nlibfoo\n",
        brew.BREW_CLEANUP_DRY_RUN_COMMAND: cleanup_output,
    }
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=lambda argv, **_: make_completed(stdout=outputs[tuple(argv)]),
    )
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        side_effect=lambda argv, **_: make_completed(stdout=outputs[tuple(argv)]),
    )

    # When
    report = measure_plugin(
        brew.PLUGIN, include_dangerous=False, settings=Config().settings_for(brew.PLUGIN)
    )

    # Then the total size is cleanup's total plus the formula folder, less the old version
    rack = old.parent
    assert report.total_size_bytes == 1024 + disk_usage([rack]) - disk_usage([old])
    sizes = {r.action.name: r.size_bytes for r in report.actions}
    assert sizes == {"cleanup": 1024, "autoremove": disk_usage([rack])}


def test_brew_total_size_falls_back_to_cleanup_when_autoremove_fails(
    mocker: MockerFixture,
) -> None:
    """Verify the total size keeps cleanup's total when the autoremove dry run fails."""
    # Given
    mocker.patch(
        "purger.plugins.brew.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["brew"]),
    )
    output = "==> This operation would free approximately 2KB of disk space.\n"

    # When / Then
    assert brew.bytes_freed_by_both_actions(output) == 2048


_BREW = "/opt/homebrew/Library/Homebrew"


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/bin/bash", "-p", f"{_BREW}/brew.sh", "install", "jq"], True),
        (
            [
                f"{_BREW}/vendor/portable-ruby/current/bin/ruby",
                "-W1",
                f"{_BREW}/brew.rb",
                "upgrade",
            ],
            True,
        ),
        (["/opt/homebrew/bin/brew", "update"], True),
        (["/opt/homebrew/bin/brewx"], False),
    ],
)
def test_brew_skip_while_running_matches_brew_runs(cmdline: list[str], expected: bool) -> None:
    """Verify brew counts as busy however it was launched, and nothing else does."""
    # Given
    assert brew.PLUGIN.skip_while_running is not None

    # When / Then
    assert brew.PLUGIN.skip_while_running(cmdline) is expected
