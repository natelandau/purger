"""Tests for the plugin data model."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from purger.plugin import (
    Action,
    DeletePaths,
    OperatingSystem,
    Plugin,
    PluginSettings,
    RiskLevel,
    RunCommand,
    Setting,
    SizeBreakdownRow,
    SizeComputedBy,
    SizeOfDirectoryPrintedBy,
    SizeOfPaths,
    SizeReportedByCommand,
    SizeUnknown,
    at_least,
    directory_from_environment,
    folder_from_environment,
    once_per_run,
    program_matching,
    reset_run_caches,
    user_cache_folders,
)
from tests.conftest import make_action, make_plugin


@pytest.mark.parametrize("name", ["Derived_Data", "derived data", "-a", "a-", "", "abc\n"])
def test_action_rejects_non_kebab_name(name: str) -> None:
    """Verify action names must be kebab-case."""
    # Given / When / Then
    with pytest.raises(ValueError, match="kebab-case"):
        make_action(name=name)


@pytest.mark.parametrize("name", ["Xcode", "xcode\n"])
def test_plugin_rejects_non_kebab_name(name: str) -> None:
    """Verify plugin names must be kebab-case, with nothing trailing."""
    # Given / When / Then
    with pytest.raises(ValueError, match="kebab-case"):
        make_plugin(name=name)


def test_names_accept_kebab_case() -> None:
    """Verify kebab-case names are accepted."""
    # Given / When
    action = make_action(name="derived-data")
    plugin = make_plugin(name="xcode-tools")

    # Then
    assert action.name == "derived-data"
    assert plugin.name == "xcode-tools"


def test_plugin_rejects_duplicate_action_names() -> None:
    """Verify duplicate action names within a plugin are rejected."""
    # Given / When / Then
    with pytest.raises(ValueError, match="duplicate"):
        make_plugin(actions=(make_action(name="a"), make_action(name="a")))


@pytest.mark.parametrize("description", ["", "   "])
def test_plugin_rejects_blank_description(description: str) -> None:
    """Verify every plugin explains itself, since the info table shows the description."""
    # Given / When / Then
    with pytest.raises(ValueError, match="description="):
        make_plugin(description=description)


def test_plugin_rejects_empty_actions() -> None:
    """Verify a plugin needs at least one action."""
    # Given / When / Then
    with pytest.raises(ValueError, match="at least one action"):
        make_plugin(actions=())


def _setting(**overrides: Any) -> Setting:
    """Build a valid Setting, overriding only the fields a test cares about."""
    fields: dict[str, Any] = {
        "name": "max_depth",
        "value_type": int,
        "default": 3,
        "description": "Depth.",
    }
    fields.update(overrides)
    return Setting(**fields)


def _reject_every_value(_: object) -> None:
    """Act as a setting validate function that rejects any value."""
    msg = "nope"
    raise ValueError(msg)


@pytest.mark.parametrize("name", ["Max", "max-depth", "_x", "", "a__b"])
def test_setting_rejects_non_snake_name(name: str) -> None:
    """Verify setting names must be snake_case."""
    # Given / When / Then
    with pytest.raises(ValueError, match="snake_case"):
        _setting(name=name)


@pytest.mark.parametrize(
    ("value_type", "default"),
    [(int, True), (int, "3"), (bool, 1), (str, 1), (list[str], (1,))],
)
def test_setting_rejects_default_of_wrong_type(value_type: object, default: object) -> None:
    """Verify a default must match the setting's value type, and a bool is not an int."""
    # Given / When / Then
    with pytest.raises(ValueError, match="does not match value_type"):
        _setting(value_type=value_type, default=default)


def test_setting_rejects_blank_description() -> None:
    """Verify a setting needs a description."""
    # Given / When / Then
    with pytest.raises(ValueError, match="description"):
        _setting(description="  ")


def test_plugin_rejects_duplicate_setting_names() -> None:
    """Verify a plugin cannot declare the same setting twice."""
    # Given / When / Then
    with pytest.raises(ValueError, match="duplicate setting"):
        make_plugin(settings=(_setting(), _setting()))


def test_setting_rejects_default_failing_its_validate() -> None:
    """Verify a default that its own validate function rejects fails at declaration."""
    # Given / When / Then
    with pytest.raises(ValueError, match="fails its own validate"):
        _setting(validate=_reject_every_value)


def test_size_breakdown_row_rejects_blank_label() -> None:
    """Verify a size breakdown row needs a label to show."""
    # Given / When / Then
    with pytest.raises(ValueError, match="label"):
        SizeBreakdownRow(label=" ", size_bytes=1)


def test_run_command_keeps_argv_and_sudo() -> None:
    """Verify RunCommand stores its words as a tuple and its sudo flag."""
    # Given / When
    command = RunCommand("a", "b", with_sudo=True)

    # Then
    assert command.argv == ("a", "b")
    assert command.with_sudo is True


def test_run_command_rejects_empty_argv() -> None:
    """Verify RunCommand with no words is rejected."""
    # Given / When / Then
    with pytest.raises(ValueError, match="argv"):
        RunCommand()


def test_delete_paths_needs_globs_or_found_by() -> None:
    """Verify DeletePaths needs exactly one of globs or found_by."""

    # Given
    def found_by(settings: object) -> list[Path]:
        return []

    # When / Then
    assert DeletePaths("~/x").globs == ("~/x",)
    with pytest.raises(ValueError, match="exactly one"):
        DeletePaths("~/x", found_by=found_by)
    with pytest.raises(ValueError, match="exactly one"):
        DeletePaths()


def test_size_of_paths_needs_globs_or_found_by() -> None:
    """Verify SizeOfPaths needs exactly one of globs or found_by."""

    # Given
    def found_by(settings: object) -> list[Path]:
        return []

    # When / Then
    assert SizeOfPaths(found_by=found_by).found_by is found_by
    with pytest.raises(ValueError, match="exactly one"):
        SizeOfPaths("~/x", found_by=found_by)
    with pytest.raises(ValueError, match="exactly one"):
        SizeOfPaths()


def test_size_unknown_rejects_blank_reason() -> None:
    """Verify SizeUnknown needs a reason."""
    # Given / When / Then
    with pytest.raises(ValueError, match="reason"):
        SizeUnknown(" ")


def test_size_reported_by_command_rejects_empty_command() -> None:
    """Verify SizeReportedByCommand needs a command."""
    # Given / When / Then
    with pytest.raises(ValueError, match="command"):
        SizeReportedByCommand((), read_size=int)


def test_at_least_rejects_smaller_values() -> None:
    """Verify at_least accepts values at or above the minimum and rejects others."""
    # Given
    check = at_least(1)

    # When / Then
    with pytest.raises(ValueError, match="must be at least 1"):
        check(0)
    assert check(1) is None
    with pytest.raises(ValueError, match="must be at least 1"):
        check(True)  # noqa: FBT003


def test_operating_system_values() -> None:
    """Verify the operating system values match sys.platform names."""
    # Given / When / Then
    assert OperatingSystem.MACOS.value == "darwin"
    assert OperatingSystem.LINUX.value == "linux"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "~/.cargo"),
        ("", "~/.cargo"),
        ("/opt/data/", "/opt/data"),
        ("/Users/me/odd[dir]", "/Users/me/odd[[]dir]"),
        ("relative/data", "~/.cargo"),
    ],
)
def test_directory_from_environment_prefers_the_variable(
    value: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the variable wins when set, with glob characters escaped, and the default fills in."""
    # Given
    monkeypatch.delenv("TOOL_HOME", raising=False)
    if value is not None:
        monkeypatch.setenv("TOOL_HOME", value)

    # When / Then
    assert directory_from_environment("TOOL_HOME", default="~/.cargo") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "~/.cargo"),
        ("", "~/.cargo"),
        ("/", "~/.cargo"),
        ("/Users/me/odd[dir]/", "/Users/me/odd[dir]"),
        ("~/data", "~/data"),
        ("relative/data", "~/.cargo"),
    ],
)
def test_folder_from_environment_returns_a_literal_path(
    value: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the variable wins with glob characters left as they are, and the default fills in."""
    # Given
    monkeypatch.delenv("TOOL_HOME", raising=False)
    if value is not None:
        monkeypatch.setenv("TOOL_HOME", value)

    # When / Then
    assert folder_from_environment("TOOL_HOME", default="~/.cargo") == expected


def test_program_matching_ignores_listed_subcommands() -> None:
    """Verify the matcher sees the program behind an interpreter and skips ignored subcommands."""
    # Given
    matches = program_matching("npm", ignoring_subcommands={"exec"})

    # When / Then
    assert matches(["npm", "install"]) is True
    assert matches(["node", "/usr/local/bin/npm", "install"]) is True
    assert matches(["npm", "exec", "x"]) is False
    assert matches(["cargo", "build"]) is False


def test_size_of_directory_printed_by_rejects_empty_command() -> None:
    """Verify SizeOfDirectoryPrintedBy needs a command."""
    # Given / When / Then
    with pytest.raises(ValueError, match="command"):
        SizeOfDirectoryPrintedBy(())


def test_plugin_defaults_to_both_operating_systems() -> None:
    """Verify a plugin that names no operating system supports both."""
    # When
    plugin = make_plugin()

    # Then
    assert plugin.operating_systems == (OperatingSystem.MACOS, OperatingSystem.LINUX)


def test_plugin_stores_gating_fields() -> None:
    """Verify the gating fields keep the values a plugin sets."""
    # Given
    available = lambda: True  # noqa: E731
    busy = lambda argv: bool(argv)  # noqa: E731

    # When
    plugin = make_plugin(
        operating_systems=(OperatingSystem.LINUX,),
        requires_on_path="tool",
        skip_unless=available,
        skip_while_running=busy,
    )

    # Then
    assert plugin.operating_systems == (OperatingSystem.LINUX,)
    assert plugin.requires_on_path == "tool"
    assert plugin.skip_unless is available
    assert plugin.skip_while_running is busy


@pytest.mark.parametrize(
    "operating_systems",
    [(), (OperatingSystem.LINUX, OperatingSystem.LINUX)],
)
def test_plugin_rejects_empty_or_repeated_operating_systems(
    operating_systems: tuple[OperatingSystem, ...],
) -> None:
    """Verify operating_systems must be non-empty with no repeats."""
    with pytest.raises(ValueError, match="operating_systems"):
        make_plugin(operating_systems=operating_systems)


def test_setting_accepts_python_value_types() -> None:
    """Verify value_type takes Python types and a list default is stored as a tuple."""
    # Given / When
    paths = Setting(name="x", value_type=list[str], default=[], description="d")
    depth = Setting(name="y", value_type=int, default=1, description="d", validate=at_least(1))

    # Then
    assert paths.default == ()
    assert depth.value_type is int


def test_setting_survives_replace() -> None:
    """Verify a replaced copy of a setting still builds and compares equal."""
    # Given
    setting = Setting(
        name="x", value_type=list[str], default=[], description="Folders.", validate=lambda _: None
    )

    # When
    copy = replace(setting)

    # Then
    assert copy == setting
    assert copy.default == ()


@pytest.mark.parametrize(("value_type", "default"), [(int, 1), (bool, False)])
def test_setting_rejects_required_on_int_or_bool(value_type: type, default: object) -> None:
    """Verify only str and list[str] settings can be required."""
    # Given / When / Then
    with pytest.raises(ValueError, match="cannot be required"):
        _setting(value_type=value_type, default=default, required=True)


def test_setting_rejects_unsupported_value_type() -> None:
    """Verify a value_type outside the four supported ones is an error."""
    # Given / When / Then
    with pytest.raises(ValueError, match="value_type must be"):
        _setting(value_type=float, default=1.0)


def test_plugin_settings_typed_accessors() -> None:
    """Verify each accessor returns its type and the wrong accessor raises TypeError."""
    # Given
    settings = PluginSettings(
        plugin="p", values={"flag": True, "name": "n", "depth": 2, "paths": ("a",)}
    )

    # Then
    assert settings.get_boolean("flag") is True
    assert settings.get_string("name") == "n"
    assert settings.get_integer("depth") == 2
    assert settings.get_string_list("paths") == ("a",)
    with pytest.raises(TypeError, match="not a string"):
        settings.get_string("depth")
    with pytest.raises(TypeError, match="not a boolean"):
        settings.get_boolean("depth")


def _new_action(**overrides: Any) -> Action:
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


def test_run_command_action_needs_size() -> None:
    """Verify an action that runs a command must say how its size is measured."""
    # Given / When / Then
    with pytest.raises(ValueError, match="size="):
        _new_action(size=None)


def test_delete_paths_action_rejects_size() -> None:
    """Verify an action that deletes paths takes its size from those paths."""
    # Given / When / Then
    with pytest.raises(ValueError, match="remove size="):
        _new_action(cleanup=DeletePaths("~/x"), size=SizeComputedBy(lambda: 1))


def test_delete_paths_action_rejects_explain_failure() -> None:
    """Verify explain_failure applies only to an action that runs a command."""
    # Given / When / Then
    with pytest.raises(ValueError, match="explain_failure="):
        _new_action(cleanup=DeletePaths("~/x"), size=None, explain_failure=lambda _: None)


def test_adds_to_plugin_total_size_needs_a_plugin_total_size() -> None:
    """Verify an action can add to the plugin total only when the plugin sets one."""
    # Given
    action = _new_action(adds_to_plugin_total_size=True)

    # When / Then
    with pytest.raises(ValueError, match="total_size="):
        Plugin(name="p", description="d", actions=(action,))
    plugin = Plugin(
        name="p", description="d", actions=(action,), total_size=SizeComputedBy(lambda: 1)
    )
    assert plugin.actions == (action,)


@pytest.mark.parametrize("description", ["", "   "])
def test_action_needs_a_description(description: str) -> None:
    """Verify every action explains itself, since prompts show the description."""
    # Given / When / Then
    with pytest.raises(ValueError, match="description="):
        _new_action(description=description)


def test_once_per_run_separates_settings() -> None:
    """Verify a cached scan for one settings value is never returned for another."""
    # Given
    calls: list[PluginSettings] = []

    @once_per_run
    def scan(settings: PluginSettings) -> tuple[str, ...]:
        calls.append(settings)
        return settings.get_string_list("paths")

    first = PluginSettings(plugin="p", values={"paths": ("a",)})
    second = PluginSettings(plugin="p", values={"paths": ("b",)})
    other_plugin = PluginSettings(plugin="q", values={"paths": ("a",)})

    # When
    results = [scan(first), scan(second), scan(first), scan(other_plugin)]

    # Then
    assert results == [("a",), ("b",), ("a",), ("a",)]
    assert len(calls) == 3


def test_reset_run_caches_forgets_results() -> None:
    """Verify a new purger command scans again."""
    # Given
    calls: list[PluginSettings] = []

    @once_per_run
    def scan(settings: PluginSettings) -> int:
        calls.append(settings)
        return len(calls)

    settings = PluginSettings(plugin="p", values={})
    assert scan(settings) == 1

    # When
    reset_run_caches()

    # Then
    assert scan(settings) == 2


def test_once_per_run_remembers_a_failure() -> None:
    """Verify a scan that raises is not repeated for every caller in the same run."""
    # Given
    calls: list[PluginSettings] = []

    @once_per_run
    def scan(settings: PluginSettings) -> int:
        calls.append(settings)
        msg = "boom"
        raise RuntimeError(msg)

    settings = PluginSettings(plugin="p", values={})

    # When / Then
    for _ in range(2):
        with pytest.raises(RuntimeError, match="boom"):
            scan(settings)
    assert len(calls) == 1


def test_once_per_run_rejects_a_generator() -> None:
    """Verify a shared scan that returns a generator fails, since later callers would find it empty."""

    # Given
    @once_per_run
    def scan(settings: PluginSettings) -> Iterator[Path]:
        return (Path(name) for name in settings.get_string_list("paths"))

    settings = PluginSettings(plugin="p", values={"paths": ("/a",)})

    # When / Then
    for _ in range(2):
        with pytest.raises(TypeError, match="not a one-pass iterator"):
            scan(settings)


@pytest.mark.parametrize(
    "build",
    [
        lambda: RunCommand(cast("Any", ("uv", "cache", "prune"))),
        lambda: DeletePaths(cast("Any", ("~/a/*", "~/b/*"))),
        lambda: SizeOfPaths(cast("Any", ["~/a/*"])),
        lambda: SizeOfDirectoryPrintedBy(cast("Any", "go env GOCACHE")),
        lambda: SizeOfDirectoryPrintedBy(("go", "env", "GOCACHE"), subpaths=cast("Any", "x")),
        lambda: SizeReportedByCommand(cast("Any", "uv cache size"), read_size=int),
    ],
    ids=["argv-tuple", "globs-tuple", "globs-list", "command-str", "subpaths-str", "size-cmd-str"],
)
def test_commands_and_globs_must_be_strings(build: Callable[[], object]) -> None:
    """Verify a command or glob list passed as one tuple or string fails at import, not mid-run."""
    # Given / When / Then
    with pytest.raises(ValueError, match="must be strings"):
        build()


def test_once_per_run_failure_traceback_does_not_grow() -> None:
    """Verify raising a remembered failure again leaves its traceback the same length."""

    # Given a scan that has failed once
    @once_per_run
    def scan(settings: PluginSettings) -> int:
        msg = "boom"
        raise RuntimeError(msg)

    settings = PluginSettings(plugin="p", values={})
    lengths: list[int] = []

    # When it is called repeatedly
    for _ in range(3):
        with pytest.raises(RuntimeError, match="boom") as caught:
            scan(settings)
        depth = 0
        frame = caught.value.__traceback__
        while frame is not None:
            depth += 1
            frame = frame.tb_next
        lengths.append(depth)

    # Then
    assert len(set(lengths[1:])) == 1


def test_once_per_run_failure_keeps_no_frame_locals() -> None:
    """Verify a remembered failure keeps its stack lines but drops the scan's local variables."""

    # Given a scan that fails while holding a large local
    @once_per_run
    def scan(settings: PluginSettings) -> int:
        found = ["path"] * 1000
        msg = f"boom after {len(found)}"
        raise RuntimeError(msg)

    settings = PluginSettings(plugin="p", values={})

    # When
    with pytest.raises(RuntimeError, match="boom") as caught:
        scan(settings)

    # Then the failing frame is still in the traceback, without its locals
    frame = caught.value.__traceback__
    names: list[str] = []
    while frame is not None:
        names.append(frame.tb_frame.f_code.co_name)
        if frame.tb_frame.f_code.co_name == "scan":
            assert "found" not in frame.tb_frame.f_locals
        frame = frame.tb_next
    assert "scan" in names


def test_cleanup_command_names_the_command_only_for_run_command() -> None:
    """Verify `cleanup_command` returns a `RunCommand` cleanup and None for `DeletePaths`."""
    # Given
    command = RunCommand("tool", "prune", with_sudo=True)
    runs = make_action(cleanup=command, size=SizeUnknown(reason="r"))
    deletes = make_action(cleanup=DeletePaths("~/x/*"))

    # When / Then
    assert runs.cleanup_command is command
    assert runs.runs_with_sudo
    assert deletes.cleanup_command is None
    assert not deletes.runs_with_sudo


def test_user_cache_folders_lists_each_location_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the default locations collapse when `XDG_CACHE_HOME` is unset."""
    # Given
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    # When / Then
    assert user_cache_folders("pip") == ("~/Library/Caches/pip", "~/.cache/pip")


def test_user_cache_folders_follows_xdg_and_the_macos_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a moved cache adds its folder, escaped, and the macOS folder takes its own name."""
    # Given
    monkeypatch.setenv("XDG_CACHE_HOME", "/data/odd[cache]")

    # When
    folders = user_cache_folders("yarn", macos_folder_name="Yarn")

    # Then
    assert folders == ("~/Library/Caches/Yarn", "~/.cache/yarn", "/data/odd[[]cache]/yarn")
