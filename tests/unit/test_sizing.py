"""Tests for size measurement."""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from nclutils.sh import CompletedCommand, ShellCommandTimeoutError
from pytest_mock import MockerFixture

from purger import sizing
from purger.config import Config, PluginSettings
from purger.plugin import Detail, PathSizes, Plugin, Setting, Tier
from purger.sizing import (
    NotConfiguredError,
    PreparedPaths,
    disk_usage,
    disk_usage_by_path,
    expand_paths,
    measure_action,
    measure_footprint,
    measure_plugin,
    parse_human_size,
    printed_dir,
    printed_dir_usage,
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


def test_measure_action_hook_wins() -> None:
    """Verify the size hook takes precedence over paths."""
    # Given an action with a hook and paths
    action = make_action(size=lambda: 42, paths=("/nonexistent",))

    # When measured / Then the hook value is used
    assert measure_action(action).size_bytes == 42


def test_measure_action_size_cmd() -> None:
    """Verify size_cmd output is parsed by default or custom parser."""
    # Given size commands
    plain = make_action(size_cmd=("echo", "1234"))
    custom = make_action(size_cmd=("echo", "1234"), parse_size=lambda _: 7)

    # When measured / Then results match
    assert measure_action(plain).size_bytes == 1234
    assert measure_action(custom).size_bytes == 7


def _boom() -> int | None:
    msg = "boom"
    raise RuntimeError(msg)


def _boom_parser(output: str) -> int:
    msg = f"cannot parse {output!r}"
    raise ValueError(msg)


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_cmd": ("false",)},
        {"size_cmd": ("echo", "garbage")},
        {"size": _boom},
        {"size_cmd": ("purger-no-such-binary",)},
    ],
)
def test_measure_action_failure_is_unknown(overrides: dict[str, object]) -> None:
    """Verify failing size sources yield an unknown size."""
    # Given a failing size source / When measured / Then None
    assert measure_action(make_action(**overrides)).size_bytes is None


def test_measure_action_paths(tmp_path: Path) -> None:
    """Verify path actions report disk usage and resolved paths."""
    # Given a directory with files
    (tmp_path / "a").write_bytes(b"x" * 10_000)
    (tmp_path / "b").write_bytes(b"x" * 10_000)
    action = make_action(command=None, paths=(str(tmp_path / "*"),))

    # When measured
    report = measure_action(action)

    # Then size and matches are reported
    assert report.resolved_paths == [tmp_path / "a", tmp_path / "b"]
    assert report.size_bytes == disk_usage(report.resolved_paths)
    assert report.size_bytes is not None
    assert report.size_bytes >= 20_000


def test_measure_action_calls_paths_function_at_measure_time(tmp_path: Path) -> None:
    """Verify a paths function runs when the action is measured, not when it is declared."""
    # Given a paths function declared before its files exist
    action = make_action(command=None, paths=lambda: (str(tmp_path / "*"),))
    (tmp_path / "a").write_bytes(b"x" * 10_000)

    # When measured
    report = measure_action(action)

    # Then the files created after the declaration are found
    assert report.resolved_paths == [tmp_path / "a"]
    assert report.size_bytes is not None
    assert report.size_bytes >= 10_000


def test_measure_action_paths_function_returning_nothing_is_empty() -> None:
    """Verify a paths function with nothing to list sizes the action as empty."""
    # Given / When
    report = measure_action(make_action(command=None, paths=lambda: ()))

    # Then
    assert report.resolved_paths == []
    assert report.size_bytes == 0


def test_measure_action_failing_paths_function_is_unknown() -> None:
    """Verify a paths function that raises leaves no paths and an unknown size."""

    # Given
    def failing_paths() -> tuple[str, ...]:
        msg = "boom"
        raise RuntimeError(msg)

    # When
    report = measure_action(make_action(command=None, paths=failing_paths))

    # Then
    assert report.resolved_paths == []
    assert report.size_bytes is None


def test_measure_action_command_without_size_is_unknown() -> None:
    """Verify a bare command action has unknown size."""
    # Given a command-only action / When measured / Then None
    assert measure_action(make_action()).size_bytes is None


def test_measure_plugin_footprint() -> None:
    """Verify footprint comes from the plugin or the sum of known action sizes."""
    # Given a plugin with its own size_cmd, one summing actions, and one with all unknown
    own = make_plugin(size_cmd=("echo", "100"), actions=(make_action(size=lambda: 5),))
    summed = make_plugin(actions=(make_action(name="a", size=lambda: 5), make_action(name="b")))
    unknown = make_plugin()

    # When
    footprints = [
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).footprint_bytes
        for plugin in (own, summed, unknown)
    ]

    # Then
    assert footprints == [100, 5, None]


def test_measure_plugin_filters_dangerous() -> None:
    """Verify dangerous actions appear only when included."""
    # Given a plugin with a dangerous action
    plugin = make_plugin(
        actions=(make_action(name="a"), make_action(name="d", tier=Tier.DANGEROUS)),
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
    action = make_action(size_cmd=("true",), parse_size=lambda s: int(s.split()[1]))

    # When measured / Then None
    assert measure_action(action).size_bytes is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_cmd": ("echo", "-5")},
        {"size_cmd": ("echo", "1"), "parse_size": lambda _: -1},
        {"size": lambda: -3},
    ],
)
def test_measure_action_negative_size_is_unknown(overrides: dict[str, object]) -> None:
    """Verify negative sizes are treated as unknown."""
    # Given a negative size source / When measured / Then None
    assert measure_action(make_action(**overrides)).size_bytes is None


def test_measure_plugin_paths_footprint(tmp_path: Path) -> None:
    """Verify plugin-level paths define the footprint."""
    # Given a plugin with its own paths
    (tmp_path / "f").write_bytes(b"x" * 10_000)
    plugin = make_plugin(paths=(str(tmp_path / "f"),))

    # When measured / Then footprint is the disk usage
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    assert report.footprint_bytes == disk_usage([tmp_path / "f"])


def test_measure_plugin_failing_size_cmd_is_unknown() -> None:
    """Verify a failing plugin size_cmd gives no footprint, with no fallback to actions."""
    # Given a failing plugin size_cmd and a known action size
    plugin = make_plugin(size_cmd=("false",), actions=(make_action(size=lambda: 5),))

    # When measured / Then footprint is None
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).footprint_bytes
        is None
    )


def test_measure_plugin_adds_actions_outside_own_footprint() -> None:
    """Verify actions outside the plugin's own footprint add to it only when they are measured."""
    # Given a plugin sizing itself plus a dangerous action whose data it does not cover
    plugin = make_plugin(
        size_cmd=("echo", "100"),
        actions=(
            make_action(name="a", size=lambda: 5),
            make_action(name="v", tier=Tier.DANGEROUS, size=lambda: 7, outside_footprint=True),
        ),
    )

    # When measured with and without dangerous actions
    with_volumes = measure_plugin(
        plugin, include_dangerous=True, settings=Config().settings_for(plugin)
    )
    without_volumes = measure_plugin(
        plugin, include_dangerous=False, settings=Config().settings_for(plugin)
    )

    # Then the outside action adds to the footprint only when it is included
    assert with_volumes.footprint_bytes == 107
    assert without_volumes.footprint_bytes == 100


def test_measure_plugin_unknown_own_footprint_stays_unknown() -> None:
    """Verify an outside action never turns an unknown own footprint into a known one."""
    # Given a failing plugin size command and an outside action with a known size
    plugin = make_plugin(
        size_cmd=("false",),
        actions=(make_action(name="v", size=lambda: 7, outside_footprint=True),),
    )

    # When / Then
    assert (
        measure_plugin(
            plugin, include_dangerous=True, settings=Config().settings_for(plugin)
        ).footprint_bytes
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


def test_size_cmd_runs_with_timeout(mocker: MockerFixture) -> None:
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
    size = measure_action(make_action(size_cmd=("slow",))).size_bytes

    # Then
    assert size is None
    assert run_command.call_args.kwargs["timeout"] > 0


def test_size_cmd_undecodable_output_is_unknown(mocker: MockerFixture) -> None:
    """Verify size command output that is not UTF-8 yields an unknown size."""
    # Given
    mocker.patch(
        "purger.sizing.run_command",
        autospec=True,
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )

    # When / Then
    assert measure_action(make_action(size_cmd=("tool",))).size_bytes is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"size_cmd": ("echo", "garbage")}, "invalid literal for int()"),
        ({"size_cmd": ("echo", "1"), "parse_size": _boom_parser}, "cannot parse"),
    ],
)
def test_size_cmd_failure_logs_the_cause(
    overrides: dict[str, object], message: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a non-integer output and a raising parser log their cause."""
    # Given
    caplog.set_level(logging.DEBUG, logger="purger.sizing")

    # When
    size = measure_action(make_action(**overrides)).size_bytes

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
    action = make_action(command=None, paths=("~/real/file", "~/link/file"))

    # When measured
    report = measure_action(action)

    # Then only the real match counts
    assert report.resolved_paths == [real / "file"]
    assert report.size_bytes == disk_usage([real / "file"])


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


def test_measure_plugin_runs_a_shared_size_cmd_once(mocker: MockerFixture) -> None:
    """Verify a size command shared by a plugin and its actions runs once per measurement."""
    # Given a plugin and two actions that read the same size command with different parsers
    result = CompletedCommand(
        argv=("size",), returncode=0, stdout="100\n", stderr="", duration=0.0, cwd=None
    )
    run_command = mocker.patch("purger.sizing.run_command", autospec=True, return_value=result)
    plugin = make_plugin(
        size_cmd=("size",),
        actions=(
            make_action(name="a", size_cmd=("size",)),
            make_action(name="b", size_cmd=("size",), parse_size=lambda out: int(out) * 2),
        ),
    )

    # When
    report = measure_plugin(plugin, include_dangerous=True, settings=Config().settings_for(plugin))

    # Then the command ran once and each parser read its output
    assert run_command.call_count == 1
    assert report.footprint_bytes == 100
    assert [a.size_bytes for a in report.actions] == [100, 200]


def test_measure_footprint_skips_actions_covered_by_own_footprint(mocker: MockerFixture) -> None:
    """Verify a self-sizing plugin's footprint measures only outside_footprint actions."""
    # Given a self-sizing plugin with one covered and one outside action
    covered = mocker.Mock(return_value=5)
    plugin = make_plugin(
        size_cmd=("echo", "100"),
        actions=(
            make_action(name="covered", size=covered),
            make_action(name="outside", size=lambda: 7, outside_footprint=True),
        ),
    )

    # When
    footprint = measure_footprint(plugin, include_dangerous=True, prepared_paths={})

    # Then the covered action is not measured and the footprint matches measure_plugin
    covered.assert_not_called()
    assert footprint == 107
    assert (
        footprint
        == measure_plugin(
            plugin, include_dangerous=True, settings=Config().settings_for(plugin)
        ).footprint_bytes
    )


@pytest.mark.parametrize("include_dangerous", [True, False])
def test_measure_footprint_sums_actions_without_own_footprint(*, include_dangerous: bool) -> None:
    """Verify a plugin without its own size sums the actions measure_plugin would include."""
    # Given a plugin sized only by its actions
    plugin = make_plugin(
        actions=(
            make_action(name="safe", size=lambda: 3),
            make_action(name="risky", tier=Tier.DANGEROUS, size=lambda: 4),
        ),
    )

    # When
    footprint = measure_footprint(plugin, include_dangerous=include_dangerous, prepared_paths={})

    # Then
    assert footprint == (7 if include_dangerous else 3)
    assert (
        footprint
        == measure_plugin(
            plugin, include_dangerous=include_dangerous, settings=Config().settings_for(plugin)
        ).footprint_bytes
    )


def test_measure_action_absolute_paths_without_home(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify absolute paths are still measured when no home directory can be determined."""
    # Given an absolute match and no known home
    (tmp_path / "file").write_bytes(b"x" * 10_000)
    mocker.patch("purger.sizing.invoking_home", autospec=True, side_effect=RuntimeError)
    action = make_action(command=None, paths=(str(tmp_path / "file"),))

    # When measured
    report = measure_action(action)

    # Then the match is kept and sized
    assert report.resolved_paths == [tmp_path / "file"]
    assert report.size_bytes == disk_usage([tmp_path / "file"])


def test_measure_plugin_size_hook_footprint() -> None:
    """Verify a plugin size hook defines the footprint ahead of size_cmd."""
    # Given a plugin with both a size hook and a size command
    plugin = make_plugin(size=lambda: 42, size_cmd=("echo", "100"))

    # When / Then the hook wins
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).footprint_bytes
        == 42
    )


def test_measure_plugin_raising_size_hook_is_unknown() -> None:
    """Verify a plugin size hook that raises gives an unknown footprint."""

    # Given
    def boom() -> int:
        msg = "no cache dir"
        raise RuntimeError(msg)

    plugin = make_plugin(size=boom, actions=(make_action(size=lambda: 5),))

    # When / Then
    assert (
        measure_plugin(
            plugin, include_dangerous=False, settings=Config().settings_for(plugin)
        ).footprint_bytes
        is None
    )


def test_printed_dir_usage_measures_whole_directory(tmp_path: Path) -> None:
    """Verify the printed directory is measured when no patterns are given."""
    # Given a tool that prints a cache directory holding a file
    (tmp_path / "f").write_bytes(b"x" * 10_000)

    # When
    size = printed_dir_usage(f"\n{tmp_path}\n")

    # Then
    assert size == disk_usage([tmp_path])


def test_printed_dir_usage_measures_only_patterns(tmp_path: Path) -> None:
    """Verify patterns narrow the measurement to matching entries."""
    # Given a cache with a matching and a non-matching subdirectory
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "f").write_bytes(b"x" * 10_000)
    (tmp_path / "fuzz").mkdir()
    (tmp_path / "fuzz" / "f").write_bytes(b"x" * 50_000)

    # When
    size = printed_dir_usage(str(tmp_path), patterns=("[0-9a-f][0-9a-f]",))

    # Then
    assert size == disk_usage([tmp_path / "ab"])


def test_printed_dir_usage_missing_directory_is_zero(tmp_path: Path) -> None:
    """Verify a printed directory that does not exist yet measures zero."""
    # Given / When / Then
    assert printed_dir_usage(str(tmp_path / "missing")) == 0


@pytest.mark.parametrize("output", ["", "off", "relative/dir"])
def test_printed_dir_usage_rejects_non_absolute_output(output: str) -> None:
    """Verify output that does not name an absolute directory is an error, so the size is unknown."""
    # Given / When / Then
    with pytest.raises(ValueError, match="absolute directory"):
        printed_dir_usage(output)


def _prepared_plugin(prepare: Callable[[PluginSettings], PreparedPaths | None]) -> Plugin:
    """Build a plugin with one setting and one prepared action named `found`."""
    return make_plugin(
        settings=(Setting(name="depth", kind="int", default=3, help="How deep to look."),),
        prepare=prepare,
        actions=(make_action(name="found", command=None, prepared=True),),
    )


def test_measure_plugin_hands_prepare_its_settings_once(tmp_path: Path) -> None:
    """Verify prepare runs once with the run's settings and its paths are measured literally."""
    # Given a prepared path whose name holds glob characters
    target = tmp_path / "a[1]"
    target.mkdir()
    (target / "data").write_bytes(b"x" * 10_000)
    seen: list[PluginSettings] = []

    def prepare(settings: PluginSettings) -> PreparedPaths:
        seen.append(settings)
        return {"found": (target,)}

    plugin = _prepared_plugin(prepare)
    settings = Config(values={"p": {"depth": 9}}).settings_for(plugin)

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=settings)

    # Then
    assert seen == [settings]
    assert seen[0].integer("depth") == 9
    assert report.prepared_paths == {"found": (target,)}
    assert report.actions[0].resolved_paths == [target]
    assert report.actions[0].size_bytes == disk_usage([target])
    assert report.footprint_bytes == disk_usage([target])


def test_prepared_paths_through_a_symlinked_parent_are_dropped(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify prepared paths pass the same symlinked-parent filter as path actions."""
    # Given
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    (tmp_path / "real" / "cache").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    kept = tmp_path / "real" / "cache"
    through_link = tmp_path / "link" / "cache"
    plugin = _prepared_plugin(lambda _: {"found": (kept, through_link)})

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.actions[0].resolved_paths == [kept]


def test_prepared_action_missing_from_the_result_measures_nothing() -> None:
    """Verify a prepared action the hook returned nothing for has no paths and size zero."""
    # Given
    plugin = _prepared_plugin(lambda _: {})

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.actions[0].resolved_paths == []
    assert report.actions[0].size_bytes == 0


def test_measure_plugin_raises_when_prepare_reports_not_configured() -> None:
    """Verify a prepare hook that returns None marks the plugin as not configured."""
    # Given
    plugin = _prepared_plugin(lambda _: None)

    # When / Then
    with pytest.raises(NotConfiguredError):
        measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))


def test_measure_footprint_remeasures_given_paths_without_preparing(tmp_path: Path) -> None:
    """Verify measure_footprint never calls prepare and re-measures the paths it is given."""
    # Given
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (first, second):
        path.mkdir()
        (path / "data").write_bytes(b"x" * 10_000)
    calls: list[str] = []

    def prepare(_: PluginSettings) -> PreparedPaths:
        calls.append("prepare")
        return {"found": (first, second)}

    plugin = _prepared_plugin(prepare)
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    before = report.footprint_bytes
    assert before is not None

    # When
    (second / "data").unlink()
    second.rmdir()
    after = measure_footprint(plugin, include_dangerous=False, prepared_paths=report.prepared_paths)

    # Then
    assert calls == ["prepare"]
    assert after == disk_usage([first])
    assert after < before


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"found": "ab"}, id="string-value"),
        pytest.param({"found": (1,)}, id="non-path-item"),
        pytest.param({"found": ("relative/cache",)}, id="relative-path"),
        pytest.param({"found": 5}, id="non-iterable-value"),
        pytest.param({1: ()}, id="non-string-key"),
        pytest.param([("found", ())], id="not-a-mapping"),
    ],
)
def test_malformed_prepare_result_is_logged_and_ignored(
    result: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify a malformed prepare result is logged like a failure and leaves no prepared paths."""
    # Given
    plugin = _prepared_plugin(lambda _: cast("PreparedPaths", result))
    caplog.set_level(logging.WARNING, logger="purger.sizing")

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.prepared_paths == {}
    assert report.actions[0].resolved_paths == []
    assert any("prepare for p failed" in r.getMessage() for r in caplog.records)


def test_prepare_result_paths_are_coerced_to_path_tuples(tmp_path: Path) -> None:
    """Verify a hook may return any iterable of strings or paths for an action."""
    # Given
    target = tmp_path / "cache"
    target.mkdir()
    plugin = _prepared_plugin(lambda _: cast("PreparedPaths", {"found": [str(target)]}))

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.prepared_paths == {"found": (target,)}
    assert report.actions[0].resolved_paths == [target]


def test_prepare_result_naming_no_prepared_action_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify paths returned for a name that is not a prepared action are logged and dropped."""
    # Given
    plugin = _prepared_plugin(lambda _: {"found": (tmp_path,), "a": (tmp_path,)})
    caplog.set_level(logging.WARNING, logger="purger.sizing")

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.prepared_paths == {"found": (tmp_path,)}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'a'" in message and "not a prepared action" in message for message in warnings)


def test_raising_prepare_does_not_abort_measurement(caplog: pytest.LogCaptureFixture) -> None:
    """Verify a prepare hook that raises is logged and the plugin shows with no prepared paths."""

    # Given
    def boom(_: PluginSettings) -> PreparedPaths:
        msg = "boom"
        raise RuntimeError(msg)

    plugin = make_plugin(
        prepare=boom,
        actions=(
            make_action(size=lambda: 5),
            make_action(name="found", command=None, prepared=True),
        ),
    )
    caplog.set_level(logging.WARNING, logger="purger.sizing")

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    footprint = measure_footprint(
        plugin, include_dangerous=False, prepared_paths=report.prepared_paths
    )

    # Then
    assert report.prepared_paths == {}
    assert report.actions[1].resolved_paths == []
    assert report.footprint_bytes == 5
    assert footprint == 5
    failures = [r for r in caplog.records if "prepare for p failed" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.WARNING]


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
    return make_plugin(actions=(make_action(command=None, paths=(str(tmp_path / "*"),)),))


def test_measure_plugin_without_details_leaves_rows_empty(tmp_path: Path) -> None:
    """Verify detail rows are only built when asked for."""
    # Given
    plugin = _files_plugin(tmp_path)

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert all(action.details == [] for action in report.actions)


def test_measure_plugin_with_details_sizes_once_and_rows_sum_to_size(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify detail rows come from the same walk as the size and add up to it."""
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
    plugin = make_plugin(actions=(make_action(size=lambda: 5),))

    # When
    report = measure_plugin(
        plugin,
        include_dangerous=False,
        settings=Config().settings_for(plugin),
        with_details=True,
    )

    # Then
    assert report.actions[0].details == []


def test_command_action_hook_that_raises_leaves_no_rows() -> None:
    """Verify a failing hook on an action without paths leaves it measured with no rows."""

    # Given a command-only action whose hook raises
    def hook(sizes: PathSizes, settings: PluginSettings) -> list[Detail]:
        msg = "boom"
        raise RuntimeError(msg)

    plugin = make_plugin(actions=(make_action(size=lambda: 5, details=hook),))

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


def test_printed_dir_reads_first_line_and_resolves(tmp_path: Path) -> None:
    """Verify the printed folder is read past blank lines and through symlinks."""
    # Given a symlink to a real folder
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)

    # When / Then
    assert printed_dir(f"\n{tmp_path / 'link'}\n") == real.resolve()


@pytest.mark.parametrize("output", ["", "gems\n"])
def test_printed_dir_rejects_missing_or_relative_output(output: str) -> None:
    """Verify an empty or relative answer is refused rather than guessed at."""
    # When / Then
    with pytest.raises(ValueError, match="absolute"):
        printed_dir(output)


def test_printed_dir_single_line_rejects_several_lines() -> None:
    """Verify a caller that needs one exact answer can refuse a multi-line one."""
    # When / Then
    with pytest.raises(ValueError, match="one absolute"):
        printed_dir("/a\n/b\n", single_line=True)
