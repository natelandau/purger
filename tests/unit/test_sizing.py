"""Tests for size measurement."""

import logging
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from nclutils.sh import CompletedCommand, ShellCommandTimeoutError
from pytest_mock import MockerFixture

from purger import sizing
from purger.clean_flow import CleanOptions, plan_clean
from purger.config import Config
from purger.plugin import (
    Action,
    DeletePaths,
    PathSizes,
    Plugin,
    PluginSettings,
    RiskLevel,
    RunCommand,
    SizeBreakdownRow,
    SizeComputedBy,
    SizeOfDirectoryPrintedBy,
    SizeOfPaths,
    SizeReportedByCommand,
    SizeUnknown,
    once_per_run,
    reset_run_caches,
)
from purger.reports import Status
from purger.sizing import (
    disk_usage,
    disk_usage_by_path,
    expand_paths,
    measure_action,
    measure_plugin,
    measure_total_size,
    parse_human_size,
    printed_directory,
)
from tests.conftest import make_action, make_plugin


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0B", 0),
        ("512", 512),
        ("861.6kB", 861_600),
        ("1.388GB", 1_388_000_000),
        ("3GB", 3_000_000_000),
        ("3.1GiB", 3_328_599_654),
        ("1.2 GiB", 1_288_490_188),
        ("10 MiB", 10_485_760),
    ],
)
def test_parse_human_size_valid(text: str, expected: int) -> None:
    """Verify human-readable sizes convert to integer bytes."""
    # Given a size string / When parsed / Then bytes match
    assert parse_human_size(text) == expected


@pytest.mark.parametrize("text", ["abc", "", "5 parsecs"])
def test_parse_human_size_invalid(text: str) -> None:
    """Verify unparsable sizes raise ValueError."""
    # Given a bad size string / When parsed / Then ValueError
    with pytest.raises(ValueError, match="size"):
        parse_human_size(text)


def test_expand_paths_globs_and_dedupes(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify globs include dotfiles, missing literals vanish, and results dedupe."""
    # Given a directory with a hidden file
    base = tmp_path / "a"
    base.mkdir()
    for name in ("one", "two", ".hidden"):
        (base / name).write_text("x")

    # When expanding overlapping and missing patterns
    found = expand_paths([str(base / "*"), str(base / "one")])
    missing = expand_paths([str(tmp_path / "nope")])

    # Then all three appear once, sorted, and the missing one is empty
    assert found == sorted([base / "one", base / "two", base / ".hidden"])
    assert missing == []

    # And ~ expands
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=tmp_path)
    assert expand_paths(["~/a/one"]) == [base / "one"]


def test_expand_paths_lists_dangling_symlink(tmp_path: Path) -> None:
    """Verify a dangling symlink literal is still listed."""
    # Given a dangling symlink
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone")

    # When expanded / Then it is listed
    assert expand_paths([str(link)]) == [link]


def test_any_path_exists_stops_at_the_first_match(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a glob, a literal, a dangling symlink, and `~` each count as existing."""
    # Given
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "one").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "gone")
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=tmp_path)

    # When / Then
    assert sizing.any_path_exists([str(tmp_path / "missing"), str(cache / "*")])
    assert sizing.any_path_exists([str(cache / "one")])
    assert sizing.any_path_exists([str(tmp_path / "link")])
    assert sizing.any_path_exists(["~/cache/*"])
    assert not sizing.any_path_exists([str(tmp_path / "missing"), str(tmp_path / "empty/*")])
    assert not sizing.any_path_exists([])


def test_any_path_exists_unresolvable_home_is_no_match(mocker: MockerFixture) -> None:
    """Verify a pattern whose ~ cannot be expanded counts as missing instead of raising."""
    # Given
    mocker.patch(
        "purger.sizing.invoking_home",
        autospec=True,
        side_effect=RuntimeError("Could not determine home directory."),
    )

    # When / Then
    assert not sizing.any_path_exists(["~/cache/*"])


def test_nonblank_lines_strips_and_drops_blank_lines() -> None:
    """Verify each line is stripped and blank or whitespace-only lines are left out."""
    # Given / When / Then
    assert sizing.nonblank_lines("  one \n\n\t\ntwo\n") == ["one", "two"]
    assert sizing.nonblank_lines("") == []


def test_disk_usage_counts_blocks_without_following_symlinks(tmp_path: Path) -> None:
    """Verify symlink targets are not counted."""
    # Given a file and a symlink to a large outside file
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * 1_000_000)
    measured = tmp_path / "measured"
    measured.mkdir()
    (measured / "f").write_bytes(b"x" * 10_000)
    (measured / "link").symlink_to(outside / "big")

    # When measured
    size = disk_usage([measured])

    # Then only the file counts
    assert 10_000 <= size < 1_000_000


def test_disk_usage_dedupes_hard_links(tmp_path: Path) -> None:
    """Verify hard links count once."""
    # Given a file and a hard link to it
    (tmp_path / "f").write_bytes(b"x" * 10_000)
    (tmp_path / "g").hardlink_to(tmp_path / "f")
    single = disk_usage([tmp_path / "f"])

    # When measuring both / Then the total equals one file
    assert disk_usage([tmp_path / "f", tmp_path / "g"]) == single


def test_disk_usage_skips_unreadable(tmp_path: Path) -> None:
    """Verify unreadable directories are skipped without raising."""
    if os.geteuid() == 0:
        pytest.skip("permissions are not enforced for root")

    # Given a readable file and an unreadable subdir
    (tmp_path / "ok").write_bytes(b"x" * 10_000)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret").write_bytes(b"x" * 1_000_000)
    locked.chmod(0o000)

    try:
        # When measured / Then the locked file is skipped
        assert 10_000 <= disk_usage([tmp_path]) < 1_000_000
    finally:
        locked.chmod(0o700)


def test_measure_action_size_reported_by_command() -> None:
    """Verify a command's output is read as bytes by the given reader."""
    # Given size commands
    plain = make_action(size=SizeReportedByCommand(("echo", "1234"), read_size=int))
    custom = make_action(size=SizeReportedByCommand(("echo", "1234"), read_size=lambda _: 7))

    # When measured / Then results match
    assert measure_action(plain, settings=_no_settings()).size_bytes == 1234
    assert measure_action(custom, settings=_no_settings()).size_bytes == 7


def _boom() -> int | None:
    msg = "boom"
    raise RuntimeError(msg)


def _boom_parser(output: str) -> int:
    msg = f"cannot parse {output!r}"
    raise ValueError(msg)


@pytest.mark.parametrize(
    "overrides",
    [
        {"size": SizeReportedByCommand(("false",), read_size=int)},
        {"size": SizeReportedByCommand(("echo", "garbage"), read_size=int)},
        {"size": SizeComputedBy(_boom)},
        {"size": SizeReportedByCommand(("purger-no-such-binary",), read_size=int)},
    ],
)
def test_measure_action_failure_is_unknown(overrides: dict[str, object]) -> None:
    """Verify failing size sources yield an unknown size."""
    # Given a failing size source / When measured / Then None
    assert measure_action(make_action(**overrides), settings=_no_settings()).size_bytes is None


def test_measure_action_paths(tmp_path: Path) -> None:
    """Verify path actions report disk usage and resolved paths."""
    # Given a directory with files
    (tmp_path / "a").write_bytes(b"x" * 10_000)
    (tmp_path / "b").write_bytes(b"x" * 10_000)
    action = make_action(cleanup=DeletePaths(str(tmp_path / "*")))

    # When measured
    report = measure_action(action, settings=_no_settings())

    # Then size and matches are reported
    assert report.resolved_paths == [tmp_path / "a", tmp_path / "b"]
    assert report.size_bytes == disk_usage(report.resolved_paths)
    assert report.size_bytes is not None
    assert report.size_bytes >= 20_000


def test_measure_action_calls_found_by_at_measure_time(tmp_path: Path) -> None:
    """Verify `found_by` runs when the action is measured, not when it is declared."""
    # Given a `found_by` function declared before its files exist
    action = make_action(cleanup=DeletePaths(found_by=lambda _settings: sorted(tmp_path.glob("*"))))
    (tmp_path / "a").write_bytes(b"x" * 10_000)

    # When measured
    report = measure_action(action, settings=_no_settings())

    # Then the files created after the declaration are found
    assert report.resolved_paths == [tmp_path / "a"]
    assert report.size_bytes is not None
    assert report.size_bytes >= 10_000


def test_measure_action_found_by_returning_nothing_is_empty() -> None:
    """Verify a `found_by` with nothing to list sizes the action as empty."""
    # Given / When
    report = measure_action(
        make_action(cleanup=DeletePaths(found_by=lambda _settings: [])), settings=_no_settings()
    )

    # Then
    assert report.resolved_paths == []
    assert report.size_bytes == 0


def test_measure_action_command_without_size_is_unknown() -> None:
    """Verify a bare command action has unknown size."""
    # Given a command-only action / When measured / Then None
    assert measure_action(make_action(), settings=_no_settings()).size_bytes is None


def test_measure_plugin_total_size() -> None:
    """Verify total size comes from the plugin or the sum of known action sizes."""
    # Given a plugin with a total size, one summing actions, and one with all unknown
    own = make_plugin(
        total_size=SizeReportedByCommand(("echo", "100"), read_size=int),
        actions=(make_action(size=SizeComputedBy(lambda: 5)),),
    )
    summed = make_plugin(
        actions=(make_action(name="a", size=SizeComputedBy(lambda: 5)), make_action(name="b"))
    )
    unknown = make_plugin()

    # When
    total_sizes = [
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).total_size_bytes
        for plugin in (own, summed, unknown)
    ]

    # Then
    assert total_sizes == [100, 5, None]


def test_measure_plugin_filters_dangerous() -> None:
    """Verify dangerous actions appear only when included."""
    # Given a plugin with a dangerous action
    plugin = make_plugin(
        actions=(make_action(name="a"), make_action(name="d", risk_level=RiskLevel.DANGEROUS)),
    )

    # When measured both ways / Then the filter applies
    off = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    on = measure_plugin(plugin, include_dangerous=True, settings=Config().settings_for(plugin))
    assert [r.action.name for r in off.actions] == ["a"]
    assert [r.action.name for r in on.actions] == ["a", "d"]


def test_disk_usage_does_not_descend_symlinked_dir(tmp_path: Path) -> None:
    """Verify a symlinked directory is not followed."""
    # Given a symlink to a directory holding a large file
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * 1_000_000)
    measured = tmp_path / "measured"
    measured.mkdir()
    (measured / "dirlink").symlink_to(outside, target_is_directory=True)

    # When measured / Then the target is not counted
    assert disk_usage([measured]) < 1_000_000


def test_measure_action_raising_parser_is_unknown() -> None:
    """Verify a parser that raises yields an unknown size."""
    # Given a parser that fails on empty output
    action = make_action(
        size=SizeReportedByCommand(("true",), read_size=lambda s: int(s.split()[1]))
    )

    # When measured / Then None
    assert measure_action(action, settings=_no_settings()).size_bytes is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"size": SizeReportedByCommand(("echo", "-5"), read_size=int)},
        {"size": SizeReportedByCommand(("echo", "1"), read_size=lambda _: -1)},
        {"size": SizeComputedBy(lambda: -3)},
    ],
)
def test_measure_action_negative_size_is_unknown(overrides: dict[str, object]) -> None:
    """Verify negative sizes are treated as unknown."""
    # Given a negative size source / When measured / Then None
    assert measure_action(make_action(**overrides), settings=_no_settings()).size_bytes is None


def test_measure_plugin_paths_total_size(tmp_path: Path) -> None:
    """Verify a total size of paths is the disk usage of those paths."""
    # Given a plugin whose total size is measured from paths
    (tmp_path / "f").write_bytes(b"x" * 10_000)
    plugin = make_plugin(total_size=SizeOfPaths(str(tmp_path / "f")))

    # When measured / Then the total size is the disk usage
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    assert report.total_size_bytes == disk_usage([tmp_path / "f"])


def test_measure_plugin_failing_total_size_command_is_unknown() -> None:
    """Verify a failing total size command gives no total size, with no fallback to actions."""
    # Given a failing total size command and a known action size
    plugin = make_plugin(
        total_size=SizeReportedByCommand(("false",), read_size=int),
        actions=(make_action(size=SizeComputedBy(lambda: 5)),),
    )

    # When measured / Then the total size is None
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).total_size_bytes
        is None
    )


def test_measure_plugin_adds_actions_that_add_to_the_total_size() -> None:
    """Verify an action that adds to the plugin total size does so only when it is measured."""
    # Given a plugin with a total size plus a dangerous action whose data it does not cover
    plugin = make_plugin(
        total_size=SizeReportedByCommand(("echo", "100"), read_size=int),
        actions=(
            make_action(name="a", size=SizeComputedBy(lambda: 5)),
            make_action(
                name="v",
                risk_level=RiskLevel.DANGEROUS,
                size=SizeComputedBy(lambda: 7),
                adds_to_plugin_total_size=True,
            ),
        ),
    )

    # When measured with and without dangerous actions
    with_volumes = measure_plugin(
        plugin, include_dangerous=True, settings=Config().settings_for(plugin)
    )
    without_volumes = measure_plugin(
        plugin, include_dangerous=False, settings=Config().settings_for(plugin)
    )

    # Then the action adds to the total size only when it is included
    assert with_volumes.total_size_bytes == 107
    assert without_volumes.total_size_bytes == 100


def test_measure_plugin_unknown_total_size_stays_unknown() -> None:
    """Verify an added action never turns an unknown total size into a known one."""
    # Given a failing total size command and an action that adds a known size to it
    plugin = make_plugin(
        total_size=SizeReportedByCommand(("false",), read_size=int),
        actions=(
            make_action(name="v", size=SizeComputedBy(lambda: 7), adds_to_plugin_total_size=True),
        ),
    )

    # When / Then
    assert (
        measure_plugin(
            plugin, include_dangerous=True, settings=Config().settings_for(plugin)
        ).total_size_bytes
        is None
    )


def test_expand_paths_home_with_glob_characters(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a home directory whose name holds glob characters is matched literally."""
    # Given a home named with brackets that holds a cache directory
    home = tmp_path / "me[1]"
    cache = home / "cache"
    cache.mkdir(parents=True)
    (cache / "one").write_text("x")
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=home)

    # When expanding a glob and a literal under ~
    globbed = expand_paths(["~/cache/*"])
    literal = expand_paths(["~/cache/one"])

    # Then both find the file
    assert globbed == [cache / "one"]
    assert literal == [cache / "one"]


def test_expand_paths_unresolvable_home_is_no_match(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a pattern whose ~ cannot be expanded matches nothing instead of raising."""
    # Given a home directory that cannot be determined
    mocker.patch(
        "purger.sizing.invoking_home",
        autospec=True,
        side_effect=RuntimeError("Could not determine home directory."),
    )
    caplog.set_level(logging.DEBUG, logger="purger.sizing")

    # When
    found = expand_paths(["~/cache/*"])

    # Then
    assert found == []
    assert "home directory" in caplog.text


def test_size_command_runs_with_timeout(mocker: MockerFixture) -> None:
    """Verify size commands run with a timeout, and a timeout yields an unknown size."""
    # Given a size command that times out
    result = CompletedCommand(
        argv=("slow",), returncode=-9, stdout="", stderr="", duration=120.0, cwd=None
    )
    run_command = mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        side_effect=ShellCommandTimeoutError(result=result, timeout=120.0),
    )

    # When
    size = measure_action(
        make_action(size=SizeReportedByCommand(("slow",), read_size=int)), settings=_no_settings()
    ).size_bytes

    # Then
    assert size is None
    assert run_command.call_args.kwargs["timeout"] > 0


def test_size_command_undecodable_output_is_unknown(mocker: MockerFixture) -> None:
    """Verify size command output that is not UTF-8 yields an unknown size."""
    # Given
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )

    # When / Then
    assert (
        measure_action(
            make_action(size=SizeReportedByCommand(("tool",), read_size=int)),
            settings=_no_settings(),
        ).size_bytes
        is None
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"size": SizeReportedByCommand(("echo", "garbage"), read_size=int)},
            "invalid literal for int()",
        ),
        (
            {"size": SizeReportedByCommand(("echo", "1"), read_size=_boom_parser)},
            "cannot parse",
        ),
    ],
)
def test_size_command_failure_logs_the_cause(
    overrides: dict[str, object], message: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a non-integer output and a raising reader log their cause."""
    # Given
    caplog.set_level(logging.DEBUG, logger="purger.sizing")

    # When
    size = measure_action(make_action(**overrides), settings=_no_settings()).size_bytes

    # Then
    assert size is None
    assert message in caplog.text


def test_measure_action_excludes_symlinked_parent_matches(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify matches behind a symlinked directory are left out of paths and size."""
    # Given a home with a real cache and a symlinked directory holding another
    home = tmp_path / "home"
    real = home / "real"
    real.mkdir(parents=True)
    (real / "file").write_bytes(b"x" * 10_000)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file").write_bytes(b"x" * 50_000)
    (home / "link").symlink_to(elsewhere)
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=home)
    action = make_action(cleanup=DeletePaths("~/real/file", "~/link/file"))

    # When measured
    report = measure_action(action, settings=_no_settings())

    # Then only the real match counts
    assert report.resolved_paths == [real / "file"]
    assert report.size_bytes == disk_usage([real / "file"])


def test_size_of_paths_keeps_matches_behind_a_symlinked_parent(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify a `SizeOfPaths` counts every match, since the tool's own command deletes them."""
    # Given a home whose symlinked directory holds the data a command removes
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file").write_bytes(b"x" * 50_000)
    (home / "link").symlink_to(elsewhere)
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=home)
    size = SizeOfPaths("~/link/file")
    action = make_action(cleanup=RunCommand("tool", "prune"), size=size)
    plugin = make_plugin(actions=(action,), total_size=size)

    # When measured as an action size and as the plugin's total size
    report = measure_action(action, settings=_no_settings())
    total = measure_total_size(plugin, include_dangerous=False, settings=_no_settings())

    # Then both count the match, and agree
    assert report.resolved_paths == [home / "link" / "file"]
    assert report.size_bytes == disk_usage([elsewhere / "file"])
    assert total == report.size_bytes


def test_expand_paths_uses_deletion_home_not_env(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ~ expands against the same home that deletion uses, not $HOME."""
    # Given $HOME pointing elsewhere than the looked-up home
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    (tmp_path / "x").write_text("x")
    mocker.patch("purger.sizing.invoking_home", autospec=True, return_value=tmp_path)

    # When / Then
    assert expand_paths(["~/x"]) == [tmp_path / "x"]


def test_measure_plugin_runs_a_shared_size_command_once(mocker: MockerFixture) -> None:
    """Verify a size command shared by a plugin and its actions runs once per measurement."""
    # Given a plugin and two actions that read the same size command with different readers
    result = CompletedCommand(
        argv=("size",), returncode=0, stdout="100\n", stderr="", duration=0.0, cwd=None
    )
    run_command = mocker.patch("purger.sizing.run_command", autospec=True, return_value=result)
    plugin = make_plugin(
        total_size=SizeReportedByCommand(("size",), read_size=int),
        actions=(
            make_action(name="a", size=SizeReportedByCommand(("size",), read_size=int)),
            make_action(
                name="b", size=SizeReportedByCommand(("size",), read_size=lambda out: int(out) * 2)
            ),
        ),
    )

    # When
    report = measure_plugin(plugin, include_dangerous=True, settings=Config().settings_for(plugin))

    # Then the command ran once and each reader read its output
    assert run_command.call_count == 1
    assert report.total_size_bytes == 100
    assert [a.size_bytes for a in report.actions] == [100, 200]


def test_measure_total_size_skips_actions_covered_by_the_total(mocker: MockerFixture) -> None:
    """Verify a plugin with a total size re-measures only the actions it adds to that total."""
    # Given a plugin with a total size, one action it covers and one that adds to it
    covered = mocker.Mock(return_value=5)
    plugin = make_plugin(
        total_size=SizeReportedByCommand(("echo", "100"), read_size=int),
        actions=(
            make_action(name="covered", size=SizeComputedBy(covered)),
            make_action(
                name="adding", size=SizeComputedBy(lambda: 7), adds_to_plugin_total_size=True
            ),
        ),
    )

    # When
    total_size = measure_total_size(
        plugin, include_dangerous=True, settings=Config().settings_for(plugin)
    )

    # Then the covered action is not measured and the total size matches measure_plugin
    covered.assert_not_called()
    assert total_size == 107
    assert (
        total_size
        == measure_plugin(
            plugin, include_dangerous=True, settings=Config().settings_for(plugin)
        ).total_size_bytes
    )


@pytest.mark.parametrize("include_dangerous", [True, False])
def test_measure_total_size_sums_actions_without_a_total(*, include_dangerous: bool) -> None:
    """Verify a plugin without a total size sums the actions measure_plugin would include."""
    # Given a plugin sized only by its actions
    plugin = make_plugin(
        actions=(
            make_action(name="safe", size=SizeComputedBy(lambda: 3)),
            make_action(
                name="risky", risk_level=RiskLevel.DANGEROUS, size=SizeComputedBy(lambda: 4)
            ),
        ),
    )

    # When
    total_size = measure_total_size(
        plugin, include_dangerous=include_dangerous, settings=Config().settings_for(plugin)
    )

    # Then
    assert total_size == (7 if include_dangerous else 3)
    assert (
        total_size
        == measure_plugin(
            plugin, include_dangerous=include_dangerous, settings=Config().settings_for(plugin)
        ).total_size_bytes
    )


def test_measure_action_absolute_paths_without_home(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify absolute paths are still measured when no home directory can be determined."""
    # Given an absolute match and no known home
    (tmp_path / "file").write_bytes(b"x" * 10_000)
    mocker.patch("purger.sizing.invoking_home", autospec=True, side_effect=RuntimeError)
    action = make_action(cleanup=DeletePaths(str(tmp_path / "file")))

    # When measured
    report = measure_action(action, settings=_no_settings())

    # Then the match is kept and sized
    assert report.resolved_paths == [tmp_path / "file"]
    assert report.size_bytes == disk_usage([tmp_path / "file"])


def test_measure_plugin_total_size_computed_by_function() -> None:
    """Verify a total size computed by a function is the plugin's total size."""
    # Given a plugin whose total size comes from a function
    plugin = make_plugin(total_size=SizeComputedBy(lambda: 42))

    # When / Then the function's value is used
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).total_size_bytes
        == 42
    )


def test_measure_plugin_raising_total_size_function_is_unknown() -> None:
    """Verify a total size function that raises gives an unknown total size."""

    # Given
    def boom() -> int:
        msg = "no cache dir"
        raise RuntimeError(msg)

    plugin = make_plugin(
        total_size=SizeComputedBy(boom), actions=(make_action(size=SizeComputedBy(lambda: 5)),)
    )

    # When / Then
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).total_size_bytes
        is None
    )


def test_disk_usage_by_path_sums_to_disk_usage(tmp_path: Path) -> None:
    """Verify per-path sizes add up to the combined measurement."""
    # Given two folders with files
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "f").write_bytes(b"x" * 10_000)
    (second / "g").write_bytes(b"x" * 20_000)

    # When measured per path
    sizes = disk_usage_by_path([first, second])

    # Then every path is a key and the sum matches disk_usage
    assert set(sizes) == {first, second}
    assert sum(sizes.values()) == disk_usage([first, second])


def test_disk_usage_by_path_counts_shared_hard_link_once(tmp_path: Path) -> None:
    """Verify a hard link shared by two paths counts toward the first path only."""
    # Given a file in one folder and a hard link to it in another
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "f").write_bytes(b"x" * 100_000)
    (second / "f").hardlink_to(first / "f")

    # When measured per path
    sizes = disk_usage_by_path([first, second])

    # Then the shared file counts once
    assert sum(sizes.values()) == disk_usage([first, second])
    assert sizes[second] < sizes[first]


def test_disk_usage_by_path_unreadable_path_is_zero(tmp_path: Path) -> None:
    """Verify a missing path still appears, with size zero."""
    # Given / When
    sizes = disk_usage_by_path([tmp_path / "missing"])

    # Then
    assert sizes == {tmp_path / "missing": 0}


def _files_plugin(tmp_path: Path) -> Plugin:
    """Build a plugin with one path action over two files in tmp_path."""
    (tmp_path / "one").write_bytes(b"x" * 10_000)
    (tmp_path / "two").write_bytes(b"x" * 20_000)
    return make_plugin(actions=(make_action(cleanup=DeletePaths(str(tmp_path / "*"))),))


def test_measure_plugin_without_details_leaves_rows_empty(tmp_path: Path) -> None:
    """Verify size breakdown rows are only built when asked for."""
    # Given
    plugin = _files_plugin(tmp_path)

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert all(action.details == [] for action in report.actions)


def test_measure_plugin_with_details_sizes_once_and_rows_sum_to_size(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify size breakdown rows come from the same walk as the size and add up to it."""
    # Given
    plugin = _files_plugin(tmp_path)
    spy = mocker.spy(sizing, "disk_usage_by_path")

    # When
    report = measure_plugin(
        plugin,
        include_dangerous=False,
        settings=Config().settings_for(plugin),
        with_details=True,
    )

    # Then
    action = report.actions[0]
    assert spy.call_count == 1
    assert len(action.details) == 2
    assert sum(row.size_bytes or 0 for row in action.details) == action.size_bytes


def test_command_action_without_paths_has_no_rows() -> None:
    """Verify a command-only action has nothing to list."""
    # Given
    plugin = make_plugin(actions=(make_action(size=SizeComputedBy(lambda: 5)),))

    # When
    report = measure_plugin(
        plugin,
        include_dangerous=False,
        settings=Config().settings_for(plugin),
        with_details=True,
    )

    # Then
    assert report.actions[0].details == []


def test_command_action_size_breakdown_that_raises_leaves_no_rows() -> None:
    """Verify a raising size breakdown function leaves a pathless action measured with no rows."""

    # Given a command-only action whose size breakdown function raises
    def breakdown(sizes: PathSizes, settings: PluginSettings) -> list[SizeBreakdownRow]:
        msg = "boom"
        raise RuntimeError(msg)

    plugin = make_plugin(
        actions=(make_action(size=SizeComputedBy(lambda: 5), size_breakdown=breakdown),)
    )

    # When
    report = measure_plugin(
        plugin,
        include_dangerous=False,
        settings=Config().settings_for(plugin),
        with_details=True,
    )

    # Then
    assert report.actions[0].size_bytes == 5
    assert report.actions[0].details == []


def test_printed_directory_reads_first_line_and_resolves(tmp_path: Path) -> None:
    """Verify the printed folder is read past blank lines and through symlinks."""
    # Given a symlink to a real folder
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)

    # When / Then
    assert printed_directory(f"\n{tmp_path / 'link'}\n") == real.resolve()


@pytest.mark.parametrize("output", ["", "gems\n"])
def test_printed_directory_rejects_missing_or_relative_output(output: str) -> None:
    """Verify an empty or relative answer is refused rather than guessed at."""
    # When / Then
    with pytest.raises(ValueError, match="absolute"):
        printed_directory(output)


def test_printed_directory_single_line_rejects_several_lines() -> None:
    """Verify a caller that needs one exact answer can refuse a multi-line one."""
    # When / Then
    with pytest.raises(ValueError, match="one absolute"):
        printed_directory("/a\n/b\n", single_line=True)


def _new_action(**overrides: object) -> Action:
    """Build a new-style action that runs a command, overriding only what a test needs."""
    fields: dict[str, Any] = {
        "name": "a",
        "risk_level": RiskLevel.SAFE,
        "description": "Remove things.",
        "cleanup": RunCommand("true"),
        "size": SizeUnknown(reason="the tool cannot preview"),
    }
    fields.update(overrides)
    return Action(**fields)


def _no_settings() -> PluginSettings:
    return PluginSettings(plugin="p", values={})


def _printing(mocker: MockerFixture, stdout: str) -> MagicMock:
    """Mock every size command to print the given output."""
    result = CompletedCommand(
        argv=("dir",), returncode=0, stdout=stdout, stderr="", duration=0.0, cwd=None
    )
    return mocker.patch("purger.sizing.run_command", autospec=True, return_value=result)


def test_size_of_directory_printed_by_measures_subpaths(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify only the subpaths of the printed folder are counted."""
    # Given a printed cache folder with a matching and a non-matching entry
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "f").write_bytes(b"x" * 10_000)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "f").write_bytes(b"x" * 50_000)
    _printing(mocker, f"{tmp_path}\n")
    action = _new_action(size=SizeOfDirectoryPrintedBy(("tool", "dir"), subpaths=("packages",)))

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.size_bytes == disk_usage([tmp_path / "packages"])


def test_size_of_directory_printed_by_without_subpaths_measures_whole_folder(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify a printed folder with no subpaths is measured as a whole."""
    # Given a printed cache folder holding two files
    (tmp_path / "a").write_bytes(b"x" * 10_000)
    (tmp_path / "b").write_bytes(b"x" * 20_000)
    _printing(mocker, f"{tmp_path}\n")
    action = _new_action(size=SizeOfDirectoryPrintedBy(("tool", "dir")))

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.size_bytes is not None
    assert report.size_bytes >= 30_000
    assert report.size_bytes == disk_usage([tmp_path])


@pytest.mark.parametrize("output", ["", "relative/dir"])
def test_size_of_directory_printed_by_bad_output_is_unknown(
    mocker: MockerFixture, output: str
) -> None:
    """Verify output that names no absolute folder gives an unknown size, not an error."""
    # Given
    _printing(mocker, output)
    action = _new_action(size=SizeOfDirectoryPrintedBy(("tool", "dir")))

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.size_bytes is None


def test_size_of_directory_printed_by_missing_folder_is_zero(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify a printed folder that does not exist yet measures zero."""
    # Given
    _printing(mocker, str(tmp_path / "missing"))
    action = _new_action(size=SizeOfDirectoryPrintedBy(("tool", "dir"), subpaths=("*",)))
    whole = _new_action(name="b", size=SizeOfDirectoryPrintedBy(("tool", "dir")))

    # When / Then
    assert measure_action(action, settings=_no_settings()).size_bytes == 0
    assert measure_action(whole, settings=_no_settings()).size_bytes == 0


def test_shared_size_command_runs_once(mocker: MockerFixture) -> None:
    """Verify a size command read by the plugin total and an action runs once per measurement."""
    # Given
    run_command = _printing(mocker, "100\n")
    command = ("tool", "usage")
    plugin = Plugin(
        name="p",
        description="d",
        total_size=SizeReportedByCommand(command, read_size=int),
        actions=(
            _new_action(size=SizeReportedByCommand(command, read_size=lambda out: int(out) * 2)),
        ),
    )

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=_no_settings())

    # Then
    assert run_command.call_count == 1
    assert report.total_size_bytes == 100
    assert report.actions[0].size_bytes == 200


@pytest.mark.parametrize(
    "found",
    [("relative/path",), ("~/tilde",), (42,), "/a/plain/string"],
    ids=["relative", "tilde", "not-a-path", "plain-string"],
)
def test_found_by_relative_path_gives_no_paths(
    found: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a found_by result that is not a list of absolute paths leaves nothing to delete."""
    # Given
    action = _new_action(
        cleanup=DeletePaths(found_by=lambda _settings: cast("Any", found)), size=None
    )

    # When
    with caplog.at_level(logging.WARNING):
        report = measure_action(action, settings=_no_settings())

    # Then
    assert report.resolved_paths == []
    assert report.size_bytes == 0
    assert any("cannot find paths for a" in r.getMessage() for r in caplog.records)


def test_found_by_paths_count_once(tmp_path: Path) -> None:
    """Verify a path that found_by returns twice is listed and counted once."""
    # Given
    target = tmp_path / "cache"
    target.mkdir()
    action = _new_action(
        cleanup=DeletePaths(found_by=lambda _settings: [target, target]), size=None
    )

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.resolved_paths == [target]


def test_found_by_raising_gives_no_paths(caplog: pytest.LogCaptureFixture) -> None:
    """Verify a size found_by that raises leaves no paths and an unknown size, and logs why."""

    # Given
    def explode(_settings: PluginSettings) -> list[Path]:
        msg = "boom"
        raise RuntimeError(msg)

    action = _new_action(size=SizeOfPaths(found_by=explode))

    # When
    with caplog.at_level(logging.DEBUG, logger="purger.sizing"):
        report = measure_action(action, settings=_no_settings())

    # Then
    assert report.resolved_paths == []
    assert report.size_bytes is None
    assert any("boom" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_found_by_receives_the_settings_and_returns_exact_paths(tmp_path: Path) -> None:
    """Verify found_by gets the plugin's settings and its paths are taken literally."""
    # Given a path whose name holds glob characters
    target = tmp_path / "a[1]"
    target.mkdir()
    (target / "data").write_bytes(b"x" * 10_000)
    seen: list[PluginSettings] = []

    def find(settings: PluginSettings) -> list[Path]:
        seen.append(settings)
        return [target]

    settings = PluginSettings(plugin="p", values={"depth": 9})
    action = _new_action(cleanup=DeletePaths(found_by=find), size=None)

    # When
    report = measure_action(action, settings=settings)

    # Then
    assert seen == [settings]
    assert report.resolved_paths == [target]
    assert report.size_bytes == disk_usage([target])


def test_size_unknown_reports_none(mocker: MockerFixture) -> None:
    """Verify an action whose size is declared unknown runs nothing to measure it."""
    # Given
    run_command = mocker.patch("purger.sizing.run_command", autospec=True)

    # When
    report = measure_action(_new_action(), settings=_no_settings())

    # Then
    assert report.size_bytes is None
    assert run_command.call_count == 0


def test_measure_total_size_reuses_once_per_run_scan(tmp_path: Path) -> None:
    """Verify re-measuring after a cleanup reuses the scan, so freed space compares the same paths."""
    # Given a scan that counts its calls and finds two folders
    first = tmp_path / "first"
    second = tmp_path / "second"
    for folder in (first, second):
        folder.mkdir()
        (folder / "data").write_bytes(b"x" * 10_000)
    calls: list[PluginSettings] = []

    @once_per_run
    def scan(settings: PluginSettings) -> list[Path]:
        calls.append(settings)
        return [first, second]

    plugin = Plugin(
        name="p",
        description="d",
        actions=(_new_action(cleanup=DeletePaths(found_by=scan), size=None),),
    )
    settings = _no_settings()
    before = measure_plugin(plugin, include_dangerous=False, settings=settings).total_size_bytes

    # When the second folder is removed and the plugin is re-measured
    for child in second.iterdir():
        child.unlink()
    second.rmdir()
    after = measure_total_size(plugin, include_dangerous=False, settings=settings)

    # Then
    assert len(calls) == 1
    assert before is not None
    assert after is not None
    assert after < before


def test_measure_total_size_adds_only_actions_outside_the_total(mocker: MockerFixture) -> None:
    """Verify a plugin with a total size re-measures only actions it adds to that total."""
    # Given
    covered = mocker.Mock(return_value=5)
    outside = mocker.Mock(return_value=7)
    plugin = Plugin(
        name="p",
        description="d",
        total_size=SizeComputedBy(lambda: 100),
        actions=(
            _new_action(name="covered", size=SizeComputedBy(covered)),
            _new_action(
                name="outside", size=SizeComputedBy(outside), adds_to_plugin_total_size=True
            ),
        ),
    )

    # When
    total = measure_total_size(plugin, include_dangerous=False, settings=_no_settings())

    # Then
    assert total == 107
    assert covered.call_count == 0
    assert outside.call_count == 1


def test_found_by_paths_through_a_symlinked_parent_are_dropped(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify found_by paths pass the same symlinked-parent filter as globs."""
    # Given
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    (tmp_path / "real" / "cache").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    kept = tmp_path / "real" / "cache"
    through_link = tmp_path / "link" / "cache"
    action = _new_action(cleanup=DeletePaths(found_by=lambda _: [kept, through_link]), size=None)

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.resolved_paths == [kept]


def test_found_by_strings_become_paths(tmp_path: Path) -> None:
    """Verify found_by may return absolute paths as strings."""
    # Given
    target = tmp_path / "cache"
    target.mkdir()
    action = _new_action(size=SizeOfPaths(found_by=lambda _: cast("list[Path]", [str(target)])))

    # When
    report = measure_action(action, settings=_no_settings())

    # Then
    assert report.resolved_paths == [target]


def test_total_size_found_by_failure_is_unknown(caplog: pytest.LogCaptureFixture) -> None:
    """Verify a plugin total sized by a failing found_by is unknown rather than an error."""

    # Given
    def explode(_settings: PluginSettings) -> list[Path]:
        msg = "boom"
        raise RuntimeError(msg)

    plugin = Plugin(
        name="p",
        description="d",
        total_size=SizeOfPaths(found_by=explode),
        actions=(_new_action(),),
    )

    # When
    with caplog.at_level(logging.DEBUG, logger="purger.sizing"):
        report = measure_plugin(plugin, include_dangerous=False, settings=_no_settings())

    # Then
    assert report.total_size_bytes is None
    assert any("cannot find paths for p" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_shared_once_per_run_failure_warns_once_for_many_delete_actions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify one failed shared scan logs a single warning across the actions that read it."""
    # Given
    reset_run_caches()
    calls: list[int] = []

    @once_per_run
    def scan(_settings: PluginSettings) -> list[Path]:
        calls.append(1)
        msg = "scan failed"
        raise RuntimeError(msg)

    actions = [
        _new_action(name=f"a{i}", cleanup=DeletePaths(found_by=scan), size=None) for i in range(4)
    ]

    # When
    with caplog.at_level(logging.DEBUG, logger="purger.sizing"):
        reports = [measure_action(action, settings=_no_settings()) for action in actions]
    reset_run_caches()

    # Then
    assert all(report.resolved_paths == [] for report in reports)
    assert len(calls) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "scan failed" in warnings[0].getMessage()


def test_delete_paths_with_raising_found_by_is_nothing_to_clean() -> None:
    """Verify a deletion whose found_by raises is never offered as a cleanup."""

    # Given
    def explode(_settings: PluginSettings) -> list[Path]:
        msg = "boom"
        raise RuntimeError(msg)

    plugin = Plugin(
        name="p",
        description="d",
        actions=(_new_action(cleanup=DeletePaths(found_by=explode), size=None),),
    )
    report = measure_plugin(plugin, include_dangerous=False, settings=_no_settings())

    # When
    approved = plan_clean(
        [report],
        CleanOptions(yes=True),
        confirm=lambda _: True,
        sudo_ready=lambda: True,
    )

    # Then
    assert approved == []
    assert report.total_size_bytes == 0
    assert report.actions[0].size_bytes == 0
    assert report.actions[0].status is Status.NOTHING_TO_CLEAN
