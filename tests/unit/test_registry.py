"""Tests for the plugin registry."""

import importlib
import logging
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import psutil
import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import (
    OperatingSystem,
    Plugin,
    PluginSettings,
    Setting,
)
from purger.registry import (
    DuplicatePluginError,
    ProcessTable,
    UnknownPluginError,
    check_plugins,
    discover_plugins,
    is_busy,
    is_configured,
    select_plugins,
    skip_reason,
)
from purger.reports import SkippedPlugin, SkipReason
from tests.conftest import make_plugin

_OWN_PID = 4242

_PLUGIN_SRC = """
from purger.plugin import Action, Plugin, RiskLevel, RunCommand, SizeUnknown

PLUGIN = Plugin(
    name={name!r},
    description="d",
    actions=(
        Action(
            name="a",
            risk_level=RiskLevel.SAFE,
            description="Remove a.",
            cleanup=RunCommand("true"),
            size=SizeUnknown(reason="test action"),
        ),
    ),
)
"""


@pytest.fixture
def build_package(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], ModuleType]]:
    """Provide a builder for uniquely named plugin packages, unloading them at teardown.

    A unique name keeps parallel tests from sharing sys.modules entries.
    """
    pkg_name = f"pkg_{tmp_path.name.replace('-', '_')}"

    def _build(modules: dict[str, str]) -> ModuleType:
        pkg_dir = tmp_path / pkg_name
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        for mod, source in modules.items():
            (pkg_dir / f"{mod}.py").write_text(textwrap.dedent(source))
        sys.path.insert(0, str(tmp_path))
        importlib.invalidate_caches()
        return importlib.import_module(pkg_name)

    yield _build

    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))
    for name in [n for n in sys.modules if n == pkg_name or n.startswith(f"{pkg_name}.")]:
        del sys.modules[name]


def test_discover_collects_plugin_constants(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify discovery returns every public module's PLUGIN keyed by name and sorted."""
    # Given a package with two plugin modules, one without PLUGIN, and one private
    package = build_package(
        {
            "zed": _PLUGIN_SRC.format(name="zed"),
            "alpha": _PLUGIN_SRC.format(name="alpha"),
            "helper": "X = 1\n",
            "_private": _PLUGIN_SRC.format(name="private"),
        },
    )

    # When discovering
    found = discover_plugins(package)

    # Then only the public plugins are returned, sorted by name
    assert list(found) == ["alpha", "zed"]


def test_discover_rejects_duplicate_names(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify two modules defining the same plugin name raise DuplicatePluginError."""
    # Given two modules with the same plugin name
    package = build_package(
        {"one": _PLUGIN_SRC.format(name="dup"), "two": _PLUGIN_SRC.format(name="dup")}
    )

    # When
    with pytest.raises(DuplicatePluginError) as exc_info:
        discover_plugins(package)

    # Then the error names the plugin and both modules
    message = str(exc_info.value)
    assert "dup" in message
    assert f"{package.__name__}.one" in message
    assert f"{package.__name__}.two" in message


def test_discover_rejects_non_plugin_constant(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify a PLUGIN that is not a Plugin raises TypeError naming the module."""
    # Given a module whose PLUGIN is a string
    package = build_package({"bad": 'PLUGIN = "nope"\n'})

    # When / Then
    with pytest.raises(TypeError, match="bad"):
        discover_plugins(package)


def test_discover_default_package_is_a_dict() -> None:
    """Verify discovery of the built-in package returns a name-ordered dict."""
    # Given / When discovering the built-in plugin package
    found = discover_plugins()

    # Then
    assert list(found) == sorted(found)


def test_skip_reason_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a plugin for another platform is skipped as PLATFORM."""
    # Given a darwin-only plugin on linux
    monkeypatch.setattr(sys, "platform", "linux")
    plugin = make_plugin(operating_systems=(OperatingSystem.MACOS,))

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is SkipReason.PLATFORM


def test_skip_reason_missing_binary(mocker: MockerFixture) -> None:
    """Verify a missing required binary is skipped as MISSING_BINARY."""
    # Given a plugin whose required binary is not on PATH
    which = mocker.patch("purger.registry.which", autospec=True, return_value=None)
    plugin = make_plugin(requires_on_path="tool")

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is SkipReason.MISSING_BINARY
    which.assert_called_once_with("tool")


def test_skip_reason_unavailable() -> None:
    """Verify a False skip_unless is skipped unless the check is disabled."""
    # Given a plugin whose skip_unless function says no
    plugin = make_plugin(skip_unless=lambda: False)

    # When
    checked = skip_reason(plugin)
    unchecked = skip_reason(plugin, check_available=False)

    # Then
    assert checked is SkipReason.UNAVAILABLE
    assert unchecked is None


def test_skip_reason_skip_unless_raises() -> None:
    """Verify a skip_unless function that raises counts as UNAVAILABLE."""

    # Given an skip_unless function that raises
    def boom() -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    # When
    reason = skip_reason(make_plugin(skip_unless=boom))

    # Then
    assert reason is SkipReason.UNAVAILABLE


def test_skip_reason_never_runs_the_configuration_check() -> None:
    """Verify skip checks leave the configuration check to measurement, which decides NOT_CONFIGURED."""
    # Given a plugin whose configuration check reports it not configured
    calls: list[str] = []

    def is_configured(_: PluginSettings) -> bool:
        calls.append("is_configured")
        return False

    plugin = make_plugin(skip_unless_configured=is_configured)

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is None
    assert calls == []


def test_skip_reason_unavailable_precedes_the_configuration_check() -> None:
    """Verify an unavailable plugin is skipped as UNAVAILABLE even when it is not configured."""
    # Given a plugin that is both unavailable and unconfigured
    plugin = make_plugin(skip_unless_configured=lambda _: False, skip_unless=lambda: False)

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is SkipReason.UNAVAILABLE


def test_skip_reason_applicable(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    """Verify an applicable plugin has no skip reason."""
    # Given a linux host where the required binary exists and skip_unless says yes
    monkeypatch.setattr(sys, "platform", "linux")
    mocker.patch("purger.registry.which", autospec=True, return_value=Path("/usr/bin/tool"))
    plugin = make_plugin(requires_on_path="tool", skip_unless=lambda: True)

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is None


def test_select_plugins() -> None:
    """Verify selection handles empty, ordered, duplicate, and unknown names."""
    # Given
    registry = {"a": make_plugin(name="a"), "b": make_plugin(name="b")}

    # When
    everything = select_plugins(registry, [])
    reordered = select_plugins(registry, ["b", "a"])
    repeated = select_plugins(registry, ["a", "a"])
    with pytest.raises(UnknownPluginError) as exc_info:
        select_plugins(registry, ["nope"])

    # Then
    assert [p.name for p in everything.plugins] == ["a", "b"]
    assert [p.name for p in reordered.plugins] == ["b", "a"]
    assert [p.name for p in repeated.plugins] == ["a"]
    assert everything.disabled == reordered.disabled == repeated.disabled == []
    assert str(exc_info.value) == "unknown plugin(s): nope. valid plugins: a, b"


def test_select_plugins_disables_skipped_plugins_in_registry_order() -> None:
    """Verify a run naming no plugins leaves skip-listed plugins out and reports them disabled."""
    # Given
    registry = {name: make_plugin(name=name) for name in ("a", "b", "c")}

    # When
    selection = select_plugins(registry, [], skip=frozenset({"c", "a"}))

    # Then
    assert [p.name for p in selection.plugins] == ["b"]
    assert selection.disabled == [
        SkippedPlugin(name="a", reason=SkipReason.DISABLED),
        SkippedPlugin(name="c", reason=SkipReason.DISABLED),
    ]


def test_select_plugins_runs_named_plugins_despite_skip() -> None:
    """Verify naming a skip-listed plugin selects it and disables nothing."""
    # Given
    registry = {name: make_plugin(name=name) for name in ("a", "b")}

    # When
    selection = select_plugins(registry, ["a"], skip=frozenset({"a"}))

    # Then
    assert [p.name for p in selection.plugins] == ["a"]
    assert selection.disabled == []


def test_disabled_plugin_checks_never_run(mocker: MockerFixture) -> None:
    """Verify selecting a disabled plugin runs none of its checks."""
    # Given
    check = mocker.Mock(return_value=True)
    registry = {"a": make_plugin(name="a", skip_unless_configured=check, skip_unless=check)}

    # When
    selection = select_plugins(registry, [], skip=frozenset({"a"}))

    # Then
    assert selection.plugins == []
    check.assert_not_called()


def test_discover_plugins_default_returns_independent_copies() -> None:
    """Verify the cached built-in registry cannot be changed through a returned dict."""
    # Given a discovered registry that a caller mutates
    first = discover_plugins()
    first.clear()

    # When discovering again
    second = discover_plugins()

    # Then the built-in plugins are still all there
    assert second
    assert second is not first


def _process(mocker: MockerFixture, *, cmdline: list[str] | None, parent_pids: list[int]) -> Mock:
    """Build a fake psutil process with an argv and a chain of parent pids."""
    parents = [mocker.Mock(pid=pid) for pid in parent_pids]
    return mocker.Mock(info={"cmdline": cmdline}, pid=99999, parents=lambda: parents)


def _is_tool(argv: list[str]) -> bool:
    return argv[0] == "tool"


@pytest.mark.parametrize(
    ("cmdline", "parent_pids", "expected"),
    [
        (["tool", "run"], [1], SkipReason.UNAVAILABLE),
        (["other"], [1], None),
        (None, [1], None),
        ([], [1], None),
        (["tool", "size"], [_OWN_PID, 1], None),
    ],
    ids=["tool-running", "other-process", "unreadable-argv", "empty-argv", "purger-subprocess"],
)
def test_skip_reason_skip_while_running(
    cmdline: list[str] | None,
    parent_pids: list[int],
    expected: SkipReason | None,
    mocker: MockerFixture,
) -> None:
    """Verify a running tool skips the plugin, but purger's own subprocesses never count."""
    # Given
    mocker.patch("purger.registry.psutil.Process", return_value=mocker.Mock(pid=_OWN_PID))
    process = _process(mocker, cmdline=cmdline, parent_pids=parent_pids)
    mocker.patch("purger.registry.psutil.process_iter", return_value=[process])

    # When
    reason = skip_reason(make_plugin(skip_while_running=_is_tool))

    # Then
    assert reason is expected


def test_skip_reason_skip_while_running_unchecked(mocker: MockerFixture) -> None:
    """Verify disabling the availability check also skips the process scan."""
    # Given
    process_iter = mocker.patch("purger.registry.psutil.process_iter")

    # When
    reason = skip_reason(make_plugin(skip_while_running=_is_tool), check_available=False)

    # Then
    assert reason is None
    process_iter.assert_not_called()


def test_skip_reason_skip_while_running_after_skip_unless(mocker: MockerFixture) -> None:
    """Verify the process scan is skipped when skip_unless already says no."""
    # Given
    process_iter = mocker.patch("purger.registry.psutil.process_iter")

    # When
    reason = skip_reason(make_plugin(skip_unless=lambda: False, skip_while_running=_is_tool))

    # Then
    assert reason is SkipReason.UNAVAILABLE
    process_iter.assert_not_called()


@pytest.mark.parametrize(
    ("still_running", "expected"),
    [(False, None), (True, SkipReason.UNAVAILABLE)],
    ids=["process-exited", "ancestor-exited"],
)
def test_skip_reason_skip_while_running_lost_during_scan(
    still_running: bool, expected: SkipReason | None, mocker: MockerFixture
) -> None:
    """Verify a vanished match is ignored, but one whose ancestor vanished still counts."""
    # Given
    process = mocker.Mock(info={"cmdline": ["tool"]}, pid=99999)
    process.parents.side_effect = psutil.NoSuchProcess(pid=99999)
    process.is_running.return_value = still_running
    mocker.patch("purger.registry.psutil.process_iter", return_value=[process])

    # When
    reason = skip_reason(make_plugin(skip_while_running=_is_tool))

    # Then
    assert reason is expected


def test_process_table_scans_once_for_many_plugins(mocker: MockerFixture) -> None:
    """Verify plugins sharing a table read the process list once."""
    # Given
    process_iter = mocker.patch(
        "purger.registry.psutil.process_iter",
        return_value=[_process(mocker, cmdline=["other"], parent_pids=[1])],
    )
    table = ProcessTable()
    plugins = [make_plugin(name=name, skip_while_running=_is_tool) for name in ("a", "b", "c")]

    # When
    reasons = [skip_reason(plugin, processes=table) for plugin in plugins]

    # Then
    assert reasons == [None, None, None]
    process_iter.assert_called_once()


@pytest.mark.parametrize(
    ("skip_while_running", "expected"),
    [(None, False), (_is_tool, True)],
    ids=["no-check", "tool-running"],
)
def test_is_busy(
    skip_while_running: Callable[[list[str]], bool] | None, expected: bool, mocker: MockerFixture
) -> None:
    """Verify is_busy reports a running tool, and plugins without a check are never busy."""
    # Given
    mocker.patch(
        "purger.registry.psutil.process_iter",
        return_value=[_process(mocker, cmdline=["tool"], parent_pids=[1])],
    )

    # When / Then
    assert is_busy(make_plugin(skip_while_running=skip_while_running)) is expected


def test_is_busy_scan_fails(mocker: MockerFixture) -> None:
    """Verify a failed scan counts as busy, so no cleanup runs blind."""
    # Given
    mocker.patch("purger.registry.psutil.process_iter", side_effect=psutil.AccessDenied())

    # When / Then
    assert is_busy(make_plugin(skip_while_running=_is_tool)) is True


def test_skip_reason_skip_while_running_scan_fails(mocker: MockerFixture) -> None:
    """Verify a process scan that fails counts as UNAVAILABLE rather than aborting the run."""
    # Given
    mocker.patch("purger.registry.psutil.process_iter", side_effect=psutil.AccessDenied())

    # When
    reason = skip_reason(make_plugin(skip_while_running=_is_tool))

    # Then
    assert reason is SkipReason.UNAVAILABLE


def test_skip_reason_reads_new_gating_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify skip_reason reads operating_systems, requires_on_path, and skip_unless."""
    # Given
    monkeypatch.setattr(sys, "platform", "linux")

    # Then
    assert (
        skip_reason(make_plugin(requires_on_path="no-such-tool-purger"))
        is SkipReason.MISSING_BINARY
    )
    assert skip_reason(make_plugin(skip_unless=lambda: False)) is SkipReason.UNAVAILABLE
    assert (
        skip_reason(make_plugin(operating_systems=(OperatingSystem.MACOS,))) is SkipReason.PLATFORM
    )


def _configured_plugin(**overrides: object) -> Plugin:
    required = Setting(
        name="paths", value_type=list[str], default=(), description="d", required=True
    )
    return make_plugin(settings=(required,), **overrides)


def test_is_configured_false_for_empty_required_setting() -> None:
    """Verify an empty required setting means not configured and a filled one passes."""
    # Given
    plugin = _configured_plugin()

    # Then
    assert not is_configured(plugin, PluginSettings(plugin="p", values={"paths": ()}))
    assert is_configured(plugin, PluginSettings(plugin="p", values={"paths": ("/a",)}))


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_is_configured_false_for_blank_required_string(blank: str) -> None:
    """Verify a required string that is empty or only whitespace counts as unset."""
    # Given a required string setting
    required = Setting(name="token", value_type=str, default="", description="d", required=True)
    plugin = make_plugin(settings=(required,))

    # Then
    assert not is_configured(plugin, PluginSettings(plugin="p", values={"token": blank}))
    assert is_configured(plugin, PluginSettings(plugin="p", values={"token": "abc"}))


def test_is_configured_uses_skip_unless_configured() -> None:
    """Verify skip_unless_configured decides when no required setting is empty, and receives the settings."""
    # Given
    plugin = make_plugin(
        settings=(Setting(name="mode", value_type=str, default="", description="d"),),
        skip_unless_configured=lambda settings: settings.get_string("mode") == "on",
    )

    # Then
    assert is_configured(plugin, PluginSettings(plugin="p", values={"mode": "on"}))
    assert not is_configured(plugin, PluginSettings(plugin="p", values={"mode": "off"}))
    assert is_configured(make_plugin(), PluginSettings(plugin="p", values={}))


def test_is_configured_false_when_skip_unless_configured_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify a raising skip_unless_configured counts as not configured and logs a warning."""

    # Given
    def boom(_: PluginSettings) -> bool:
        msg = "broken"
        raise RuntimeError(msg)

    # When
    result = is_configured(
        make_plugin(skip_unless_configured=boom), PluginSettings(plugin="p", values={})
    )

    # Then
    assert result is False
    assert "broken" in caplog.text
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def _no_settings(plugin: Plugin) -> PluginSettings:
    return Config().settings_for(plugin)


def test_check_splits_plugins_in_plugin_order(mocker: MockerFixture) -> None:
    """Verify checking keeps runnable and skipped plugins each in plugin order."""
    # Given beta and delta do not apply here
    mocker.patch(
        "purger.registry.skip_reason",
        autospec=True,
        side_effect=lambda plugin, processes: (
            SkipReason.PLATFORM if plugin.name in {"beta", "delta"} else None
        ),
    )
    plugins = [make_plugin(name=name) for name in ("alpha", "beta", "gamma", "delta")]

    # When
    usable, skipped = check_plugins(plugins, settings_for=_no_settings)

    # Then
    assert [p.name for p in usable] == ["alpha", "gamma"]
    assert [s.name for s in skipped] == ["beta", "delta"]


def test_check_shares_one_process_table(mocker: MockerFixture) -> None:
    """Verify every plugin in one scan checks for busy tools against the same process table."""
    # Given
    skip = mocker.patch("purger.registry.skip_reason", autospec=True, return_value=None)
    plugins = [make_plugin(name=name) for name in ("alpha", "beta", "gamma")]

    # When
    check_plugins(plugins, settings_for=_no_settings)

    # Then
    tables = {id(call.kwargs["processes"]) for call in skip.call_args_list}
    assert len(tables) == 1


def test_check_skips_a_plugin_whose_configuration_check_fails() -> None:
    """Verify a configuration check returning False skips the plugin as not configured."""
    # Given
    plugin = make_plugin(name="alpha", skip_unless_configured=lambda _: False)

    # When
    usable, skipped = check_plugins([plugin], settings_for=_no_settings)

    # Then
    assert usable == []
    assert skipped == [SkippedPlugin(name="alpha", reason=SkipReason.NOT_CONFIGURED)]


def test_check_does_not_check_configuration_of_a_plugin_that_is_unavailable() -> None:
    """Verify an unavailable plugin is skipped before its configuration check runs."""
    # Given
    calls: list[str] = []

    def is_configured(_: PluginSettings) -> bool:
        calls.append("is_configured")
        return False

    plugin = make_plugin(
        name="alpha", skip_unless=lambda: False, skip_unless_configured=is_configured
    )

    # When
    _, skipped = check_plugins([plugin], settings_for=_no_settings)

    # Then
    assert skipped == [SkippedPlugin(name="alpha", reason=SkipReason.UNAVAILABLE)]
    assert calls == []
