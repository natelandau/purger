"""Tests for the purger command line."""

import json
import sys
import threading
from collections.abc import Callable
from io import StringIO
from pathlib import Path

import pytest
from pytest_mock import MockerFixture, MockType
from rich.console import Console
from rich.text import Text

from purger import cli
from purger.cli import ExitCode, app, confirm, main
from purger.config import Config
from purger.measure_display import MeasureDisplay
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
    once_per_run,
)
from purger.reports import ActionReport, PluginReport, SkipReason, Status
from tests.conftest import EMPTY_SETTINGS, make_action, make_plugin, table_row

_SIZE = 100


def _fake_measure(
    plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings, with_details: bool = False
) -> PluginReport:
    """Measure every action as a fixed size, honoring the DANGEROUS filter.

    A path action resolves three paths, and with details each action gets one row.
    """
    actions = [
        ActionReport(
            action=action,
            size_bytes=_SIZE,
            resolved_paths=(
                [Path(f"/fake/{action.name}/{i}") for i in range(3)]
                if isinstance(action.cleanup, DeletePaths)
                else []
            ),
            details=[SizeBreakdownRow(label=f"row-of-{action.name}", size_bytes=_SIZE)]
            if with_details
            else [],
        )
        for action in plugin.actions
        if include_dangerous or action.risk_level is not RiskLevel.DANGEROUS
    ]
    return PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=plugin,
        total_size_bytes=_SIZE * len(actions),
        actions=actions,
    )


def _mark_done(report: ActionReport, *, home: object, verbose: bool, interactive: bool) -> None:
    report.status = Status.DONE


def _patch_measure(mocker: MockerFixture, *, side_effect: object) -> MockType:
    """Patch measuring in the CLI and re-measuring in execution with one shared mock."""
    measure = mocker.patch("purger.cli.measure_plugin", autospec=True, side_effect=side_effect)
    mocker.patch(
        "purger.execution.measure_total_size",
        autospec=True,
        side_effect=lambda plugin, *, include_dangerous, settings: (
            measure(plugin, include_dangerous=include_dangerous, settings=settings).total_size_bytes
        ),
    )
    return measure


@pytest.fixture
def patch_registry(mocker: MockerFixture) -> Callable[..., None]:
    """Replace plugin discovery and measuring with fakes built from the given plugins."""

    def _patch(*plugins: Plugin) -> None:
        mocker.patch(
            "purger.cli.discover_plugins",
            autospec=True,
            return_value={p.name: p for p in plugins},
        )
        _patch_measure(mocker, side_effect=_fake_measure)

    return _patch


def _run(*args: str) -> int:
    return app(list(args), result_action="return_value")


def _json(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def test_info_json_is_single_document(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify info --json writes exactly one JSON document to stdout."""
    # Given
    patch_registry(make_plugin(name="alpha"))

    # When
    code = _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert doc["schema_version"] == 1
    assert doc["plugins"][0]["name"] == "alpha"
    assert doc["total_bytes"] == _SIZE


def test_info_human_prints_table(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify info without --json prints a row per tool and never the action names."""
    # Given
    patch_registry(make_plugin(name="alpha", actions=(make_action(name="prune"),)))

    # When
    code = _run("info")

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert "alpha" in out
    assert "Partial" in out
    assert "prune" not in out


@pytest.mark.parametrize("command", ["info", "clean"])
def test_unknown_plugin_json_error(
    command: str, patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify an unknown plugin name exits 2 with a JSON error document."""
    # Given
    patch_registry(make_plugin(name="alpha"))

    # When
    code = _run(command, "nope", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.USAGE
    assert doc["schema_version"] == 1
    assert doc["error"]["code"] == "unknown_plugin"
    assert "alpha" in doc["error"]["message"]


def test_unknown_plugin_human_error(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify an unknown plugin name without --json exits 2 and names the valid plugins."""
    # Given
    patch_registry(make_plugin(name="alpha"))

    # When
    code = _run("info", "nope")

    # Then
    err = capsys.readouterr().err
    assert code == ExitCode.USAGE
    assert "nope" in err
    assert "alpha" in err


def test_named_unavailable_plugin_warns(
    patch_registry: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify naming a plugin for another platform warns with the current platform and exits 0."""
    # Given
    monkeypatch.setattr(sys, "platform", "linux")
    patch_registry(make_plugin(name="macish", operating_systems=(OperatingSystem.MACOS,)))

    # When
    code = _run("info", "macish", "--json")

    # Then
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert code == ExitCode.OK
    assert doc["skipped"] == [{"name": "macish", "reason": "platform"}]
    assert "macish" in captured.err
    assert "linux" in captured.err


def test_measuring_rows_only_for_plugins_that_can_run(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify only plugins that pass their checks get a measuring row."""
    # Given one plugin that runs here and one whose check fails
    patch_registry(make_plugin(name="ready"), make_plugin(name="absent", skip_unless=lambda: False))
    display = mocker.patch("purger.cli.MeasureDisplay", autospec=True)

    # When
    _run("info", "-vv")

    # Then
    display.assert_called_once_with(["ready"])


@pytest.mark.parametrize("verbose", [(), ("-v",), ("-vv",)])
def test_skipped_plugins_listed_only_when_verbose(
    verbose: tuple[str, ...],
    patch_registry: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify -v and -vv list plugins that cannot run, and a plain run does not."""
    # Given
    patch_registry(make_plugin(name="ready"), make_plugin(name="absent", skip_unless=lambda: False))

    # When
    _run("info", *verbose)

    # Then
    out = capsys.readouterr().out
    assert ("Skipped plugins" in out) is bool(verbose)
    assert ("absent: its tool is not available" in out) is bool(verbose)


def test_named_plugin_that_cannot_run_warns_without_verbose(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a plugin the user named still gets a warning when it cannot run."""
    # Given
    patch_registry(make_plugin(name="ready"), make_plugin(name="absent", skip_unless=lambda: False))

    # When
    code = _run("info", "ready", "absent")

    # Then
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == ExitCode.OK
    assert "absent is not available on this machine" in output
    assert "Skipped plugins" not in output


@pytest.mark.parametrize(
    ("argv", "is_json"),
    [
        (["purger", "info"], False),
        (["purger", "info", "--json"], True),
        (["purger", "info", "--json=true"], True),
        (["purger", "info", "--json=false"], False),
    ],
)
def test_main_refuses_root(
    argv: list[str],
    is_json: bool,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify main exits 2 before running anything when run as root."""
    # Given
    mocker.patch("purger.cli.os.geteuid", autospec=True, return_value=0)
    discover = mocker.patch("purger.cli.discover_plugins", autospec=True)
    monkeypatch.setattr(sys, "argv", argv)

    # When
    with pytest.raises(SystemExit) as exc_info:
        main()

    # Then
    captured = capsys.readouterr()
    assert exc_info.value.code == ExitCode.USAGE
    discover.assert_not_called()
    if is_json:
        assert json.loads(captured.out)["error"]["code"] == "running_as_root"
    else:
        assert "root" in captured.err
        assert captured.out == ""


def test_clean_json_never_runs_risky(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --json --yes --dangerous runs only SAFE actions and declines the rest."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha",
            actions=(
                make_action(name="safe-one", risk_level=RiskLevel.SAFE),
                make_action(name="risky", risk_level=RiskLevel.CAUTION),
                make_action(name="wipe", risk_level=RiskLevel.DANGEROUS),
            ),
        )
    )
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--json", "--yes", "--dangerous")

    # Then
    doc = _json(capsys)
    statuses = {a["name"]: a["status"] for a in doc["plugins"][0]["actions"]}
    assert code == ExitCode.OK
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["safe-one"]
    assert statuses == {"safe-one": "done", "risky": "declined", "wipe": "declined"}
    assert doc["schema_version"] == 1
    assert doc["plugins"][0]["freed_bytes"] == 0


def test_clean_json_without_yes_runs_nothing(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --json without --yes declines SAFE actions and runs nothing."""
    # Given
    patch_registry(make_plugin(name="alpha"))
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    run_action.assert_not_called()
    assert doc["plugins"][0]["actions"][0]["status"] == "declined"


def test_clean_failure_exit_code(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a failed action makes clean exit 1 and reports the error."""

    # Given
    def _fail(report: ActionReport, *, home: object, verbose: bool, interactive: bool) -> None:
        report.status = Status.FAILED
        report.error = "boom"

    patch_registry(make_plugin(name="alpha"))
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=_fail)

    # When
    code = _run("clean", "--json", "--yes")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.ACTION_FAILED
    assert doc["plugins"][0]["actions"][0]["status"] == "failed"
    assert doc["plugins"][0]["actions"][0]["error"] == "boom"


def test_clean_interrupt(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Ctrl-C during execution stops the run, skips untouched actions, and exits 130."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha",
            actions=(make_action(name="first"), make_action(name="second")),
        )
    )
    run_action = mocker.patch(
        "purger.execution.run_action", autospec=True, side_effect=KeyboardInterrupt
    )

    # When
    code = _run("clean", "--json", "--yes")

    # Then
    doc = _json(capsys)
    statuses = {a["name"]: a["status"] for a in doc["plugins"][0]["actions"]}
    assert code == ExitCode.INTERRUPTED
    assert run_action.call_count == 1
    errors = {a["name"]: a["error"] for a in doc["plugins"][0]["actions"]}
    assert statuses == {"first": "failed", "second": "skipped"}
    assert errors["first"] == "interrupted"
    assert doc["plugins"][0]["freed_bytes"] is None


def test_clean_dry_run_executes_nothing(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --dry-run lists each planned command and never runs or prompts."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha",
            actions=(
                make_action(name="prune", cleanup=RunCommand("tool", "prune")),
                make_action(
                    name="risky", risk_level=RiskLevel.CAUTION, cleanup=RunCommand("tool", "nuke")
                ),
            ),
        )
    )
    run_action = mocker.patch("purger.execution.run_action", autospec=True)
    ask = mocker.patch("purger.cli.Confirm.ask", autospec=True)

    # When
    code = _run("clean", "--dry-run")

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    run_action.assert_not_called()
    ask.assert_not_called()
    assert "tool prune" in out
    assert "tool nuke" in out


def test_clean_json_dry_run_returns_plan(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --json --dry-run marks actions planned and executes nothing."""
    # Given
    patch_registry(make_plugin(name="alpha"))
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean", "--json", "--dry-run")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    run_action.assert_not_called()
    assert doc["plugins"][0]["actions"][0]["status"] == "planned"


def test_clean_interactive_yes_still_confirms_caution_and_notes_sudo(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --yes skips only the SAFE prompt, CAUTION still asks, and sudo is announced."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha",
            actions=(
                make_action(name="sudo-safe", cleanup=RunCommand("tool", "x", with_sudo=True)),
                make_action(name="risky", risk_level=RiskLevel.CAUTION),
            ),
        )
    )
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    ask = mocker.patch("purger.cli.Confirm.ask", autospec=True, return_value=False)
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--yes")

    # Then
    err = capsys.readouterr().err
    assert code == ExitCode.OK
    assert ask.call_count == 1
    assert ask.call_args.args[0].plain.startswith("alpha (about ")
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["sudo-safe"]
    assert "alpha/sudo-safe" in err
    assert "sudo" in err


def test_clean_interrupt_at_prompt_runs_nothing(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
) -> None:
    """Verify Ctrl-C at a prompt exits 130 without running anything."""
    # Given a prompt cancelled with Ctrl-C
    patch_registry(make_plugin(name="alpha"))
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    mocker.patch("purger.cli.Confirm.ask", autospec=True, side_effect=KeyboardInterrupt)
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean")

    # Then
    assert code == ExitCode.INTERRUPTED
    run_action.assert_not_called()


def _prompt_console(
    mocker: MockerFixture, *, width: int, typed: str | list[str] | type[BaseException]
) -> tuple[StringIO, MockType]:
    """Route confirm through a real terminal rich console of a fixed width that reads `typed`.

    A list feeds one answer per prompt, and an exception type is raised at the prompt.
    """
    out = StringIO()
    console = Console(file=out, width=width, force_terminal=True, color_system=None)
    mocker.patch("purger.cli.pp.console", return_value=console)
    if isinstance(typed, str):
        read = mocker.patch("builtins.input", return_value=typed)
    else:
        read = mocker.patch("builtins.input", side_effect=typed)
    return out, read


@pytest.mark.parametrize(("typed", "expected"), [("n", False), ("y", True), ("Y", True)])
def test_confirm_maps_answers(typed: str, expected: bool, mocker: MockerFixture) -> None:
    """Verify confirm returns True only when the user answers yes."""
    # Given
    _prompt_console(mocker, width=80, typed=typed)

    # When / Then
    assert confirm("Proceed?") is expected


def test_confirm_has_no_default(mocker: MockerFixture) -> None:
    """Verify Enter alone never approves, so the question is asked again."""
    # Given a user who presses Enter, then answers yes
    _, read = _prompt_console(mocker, width=80, typed=["", "y"])

    # When / Then
    assert confirm("Proceed?") is True
    assert read.call_count == 2


def test_confirm_prints_blank_line_before_prompt(mocker: MockerFixture) -> None:
    """Verify each question starts after a blank line so consecutive questions stay apart."""
    # Given
    out, _ = _prompt_console(mocker, width=80, typed="y")

    # When
    confirm("First?")
    confirm("Second?")

    # Then
    assert out.getvalue() == "\nFirst? [y/n]: \nSecond? [y/n]: "


def test_confirm_asks_on_stderr_when_stdout_is_redirected(mocker: MockerFixture) -> None:
    """Verify a redirected stdout gets no question, so the user still sees it on the terminal."""
    # Given stdout redirected to a file and stderr on a terminal
    stdout, stderr = StringIO(), StringIO()
    mocker.patch("purger.cli.pp.console", return_value=Console(file=stdout))
    mocker.patch(
        "purger.cli.pp.err_console",
        return_value=Console(file=stderr, force_terminal=True, color_system=None),
    )
    mocker.patch("builtins.input", return_value="y")

    # When
    confirm("Proceed?")

    # Then
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "\nProceed? [y/n]: "


def test_confirm_wraps_between_words(mocker: MockerFixture) -> None:
    """Verify a long question wraps at word boundaries within the console width."""
    # Given
    message = (
        "playwright (about 1.5 GB): Remove each browser build older than the newest one. "
        "Playwright needs `playwright install` before its next run. Continue?"
    )
    out, _ = _prompt_console(mocker, width=40, typed="y")

    # When
    confirm(message)

    # Then
    lines = out.getvalue().strip("\n").split("\n")
    assert len(lines) > 1
    assert all(len(line) <= 40 for line in lines)
    assert " ".join(lines).split() == [*message.split(), "[y/n]:"]


def test_confirm_shows_brackets_literally(mocker: MockerFixture) -> None:
    """Verify square brackets in a question are shown as typed, not read as rich markup."""
    # Given
    out, _ = _prompt_console(mocker, width=80, typed="y")

    # When
    confirm("Remove [bold]cache[/bold]?")

    # Then
    assert "Remove [bold]cache[/bold]? [y/n]:" in out.getvalue()


def test_confirm_passes_text_to_prompt(mocker: MockerFixture) -> None:
    """Verify the question reaches the prompt as rich Text."""
    # Given
    ask = mocker.patch("purger.cli.Confirm.ask", autospec=True, return_value=True)

    # When
    confirm("Proceed?")

    # Then
    assert ask.call_args.args[0] == Text("Proceed?")


@pytest.mark.parametrize("error", [KeyboardInterrupt, EOFError])
def test_confirm_cancelled_prompt_is_interrupt(
    error: type[BaseException], mocker: MockerFixture
) -> None:
    """Verify Ctrl-C or closed input raises KeyboardInterrupt so the run stops."""
    # Given
    _prompt_console(mocker, width=80, typed=error)

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        confirm("Proceed?")


@pytest.mark.parametrize(
    "argv",
    [
        ["purger", "info", "--bogus", "--json"],
        ["purger", "clean", "--dry-run=maybe", "--json"],
        ["purger", "nope", "--json"],
    ],
)
def test_main_json_usage_error_is_a_document(
    argv: list[str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify an argument error with --json prints the JSON error document and exits 2."""
    # Given
    mocker.patch("purger.cli.os.geteuid", autospec=True, return_value=1000)
    monkeypatch.setattr(sys, "argv", argv)

    # When
    with pytest.raises(SystemExit) as exc_info:
        main()

    # Then
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert exc_info.value.code == ExitCode.USAGE
    assert doc["schema_version"] == 1
    assert doc["error"]["code"] == "usage"
    assert doc["error"]["message"]
    assert captured.err == ""


def test_main_usage_error_without_json_exits_2(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify an argument error without --json prints a human error and no JSON."""
    # Given
    mocker.patch("purger.cli.os.geteuid", autospec=True, return_value=1000)
    monkeypatch.setattr(sys, "argv", ["purger", "info", "--bogus"])

    # When
    with pytest.raises(SystemExit) as exc_info:
        main()

    # Then
    captured = capsys.readouterr()
    assert exc_info.value.code == ExitCode.USAGE
    assert "--bogus" in captured.err + captured.out
    assert "schema_version" not in captured.out


def test_clean_human_prints_results_table(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a human clean run ends with each tool's size before and after, and the space freed."""
    # Given
    plugin = make_plugin(name="alpha")
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    before = _fake_measure(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    after = PluginReport(settings=EMPTY_SETTINGS, plugin=plugin, total_size_bytes=40, actions=[])
    _patch_measure(mocker, side_effect=[before, after])
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    results = mocker.spy(cli, "render_results")

    # When
    code = _run("clean", "--yes")

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    for header in ("Before", "After", "Freed"):
        assert header in out
    assert table_row(results.spy_return, "alpha") == ["alpha", "100 B", "40 B", "60 B"]
    assert "Freed 60 B" not in out
    assert "Status" not in out


def test_clean_human_lists_failures_under_table(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a tool with one success and one failure gets a results row and a failure line."""
    # Given a tool whose second action fails
    plugin = make_plugin(name="alpha", actions=(make_action(name="one"), make_action(name="two")))
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    before = _fake_measure(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    after = PluginReport(settings=EMPTY_SETTINGS, plugin=plugin, total_size_bytes=50, actions=[])
    _patch_measure(mocker, side_effect=[before, after])

    def run(report: ActionReport, *, home: object, verbose: bool, interactive: bool) -> None:
        if report.action.name == "two":
            report.status = Status.FAILED
            report.error = "boom"
        else:
            report.status = Status.DONE

    mocker.patch("purger.execution.run_action", autospec=True, side_effect=run)

    # When
    code = _run("clean", "--yes")

    # Then
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == ExitCode.ACTION_FAILED
    results = captured.out[captured.out.index("Before") :]
    assert " alpha " in results
    assert "alpha: two failed: boom" in output
    assert "alpha/two failed" not in output


def test_clean_json_stdout_is_only_the_document(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --json -v keeps tool output off stdout so it parses as one document."""

    # Given a runner that writes to stdout whenever it is allowed to stream or attach
    def _noisy(report: ActionReport, *, home: object, verbose: bool, interactive: bool) -> None:
        if verbose or interactive:
            sys.stdout.write("tool output\n")
        report.status = Status.DONE

    patch_registry(
        make_plugin(
            name="alpha",
            actions=(
                make_action(name="plain"),
                make_action(name="rooty", cleanup=RunCommand("tool", "x", with_sudo=True)),
            ),
        )
    )
    mocker.patch("purger.cli.sudo_ready", autospec=True, return_value=True)
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_noisy)

    # When
    code = _run("clean", "--json", "--yes", "-v")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert run_action.call_count == 2
    for call in run_action.call_args_list:
        assert call.kwargs["verbose"] is False
        assert call.kwargs["interactive"] is False
    assert doc["schema_version"] == 1


def test_clean_human_verbose_streams(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify -v in human mode asks the runner to stream tool output."""
    # Given
    patch_registry(make_plugin(name="alpha"))
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    _run("clean", "--yes", "-v")

    # Then
    assert run_action.call_args.kwargs["verbose"] is True


def test_global_verbose_flag_before_command(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify -v before the command is accepted and reaches the runner."""
    # Given
    patch_registry(make_plugin(name="alpha"))
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = app.meta(["-v", "clean", "--yes"], result_action="return_value")

    # Then
    assert code == ExitCode.OK
    assert run_action.call_args.kwargs["verbose"] is True


def test_clean_json_interrupt_while_measuring(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify Ctrl-C during the first measurement emits a JSON error and exits 130."""
    # Given
    plugin = make_plugin(name="alpha")
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    _patch_measure(mocker, side_effect=KeyboardInterrupt)

    # When
    code = _run("clean", "--json", "--yes")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.INTERRUPTED
    assert doc["error"]["code"] == "interrupted"


def test_clean_json_interrupt_while_remeasuring(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify Ctrl-C while measuring freed space still reports results and exits 130."""
    # Given
    plugin = make_plugin(name="alpha")
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    before = _fake_measure(plugin, include_dangerous=False, settings=Config().settings_for(plugin))
    _patch_measure(mocker, side_effect=[before, KeyboardInterrupt])
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--json", "--yes")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.INTERRUPTED
    assert doc["plugins"][0]["actions"][0]["status"] == "done"
    assert doc["plugins"][0]["freed_bytes"] is None


def test_clean_interactive_caution_yes_runs_it(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify answering Yes to a CAUTION prompt runs that action interactively."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha", actions=(make_action(name="risky", risk_level=RiskLevel.CAUTION),)
        )
    )
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    mocker.patch("purger.cli.Confirm.ask", autospec=True, return_value=True)
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean")

    # Then
    assert code == ExitCode.OK
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["risky"]
    assert run_action.call_args.kwargs["interactive"] is True


def test_clean_non_interactive_warns_about_declined(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a non-interactive run without --yes names every declined action on stderr."""
    # Given
    patch_registry(
        make_plugin(
            name="alpha",
            actions=(
                make_action(name="safe-one"),
                make_action(name="risky", risk_level=RiskLevel.CAUTION),
            ),
        )
    )
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean")

    # Then
    err = capsys.readouterr().err
    assert code == ExitCode.OK
    run_action.assert_not_called()
    assert "--yes" in err
    assert "alpha/safe-one" in err
    assert "alpha/risky" in err


def test_clean_human_interrupt_while_measuring(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify Ctrl-C during the first measurement in human mode prints an error and exits 130."""
    # Given measuring is interrupted
    plugin = make_plugin(name="alpha")
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    _patch_measure(mocker, side_effect=KeyboardInterrupt)
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean", "--yes")

    # Then
    captured = capsys.readouterr()
    assert code == ExitCode.INTERRUPTED
    assert "Interrupted while measuring caches" in captured.err
    assert "schema_version" not in captured.out
    run_action.assert_not_called()


def test_main_json_equals_usage_error_is_json(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a usage error with --json=true prints the JSON error document on stdout."""
    # Given a bad flag next to --json=true
    mocker.patch("purger.cli.os.geteuid", autospec=True, return_value=1000)
    mocker.patch("purger.cli.attach_plugin_catalog", autospec=True)
    monkeypatch.setattr(sys, "argv", ["purger", "info", "--json=true", "--bogus"])

    # When
    with pytest.raises(SystemExit) as exc_info:
        main()

    # Then
    captured = capsys.readouterr()
    assert exc_info.value.code == ExitCode.USAGE
    assert json.loads(captured.out)["error"]["code"] == "usage"


def test_measure_keeps_plugin_order_when_plugins_finish_out_of_order(
    mocker: MockerFixture,
) -> None:
    """Verify concurrent measuring reports plugins in plugin order."""
    # Given alpha finishes only after beta starts
    beta_started = threading.Event()

    def measure(
        plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings, with_details: bool
    ) -> PluginReport:
        if plugin.name == "alpha":
            assert beta_started.wait(timeout=5)
        elif plugin.name == "beta":
            beta_started.set()
        return _fake_measure(plugin, include_dangerous=include_dangerous, settings=settings)

    mocker.patch("purger.cli.measure_plugin", autospec=True, side_effect=measure)
    plugins = [make_plugin(name=name) for name in ("alpha", "beta", "delta")]
    display = mocker.Mock(spec=MeasureDisplay)

    # When
    reports = cli._measure(plugins, dangerous=False, display=display, config=Config())

    # Then
    assert [r.plugin.name for r in reports] == ["alpha", "beta", "delta"]
    finished = {call.kwargs["name"] for call in display.finish.call_args_list}
    assert finished == {"alpha", "beta", "delta"}


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Write a config file and point PURGER_CONFIG at it."""

    def _write(text: str) -> None:
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("PURGER_CONFIG", str(path))

    return _write


def test_skip_setting_leaves_plugin_out(
    patch_registry: Callable[..., None],
    config_file: Callable[[str], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a plugin in the skip setting is reported as disabled and not measured."""
    # Given
    patch_registry(make_plugin(name="a"), make_plugin(name="b"))
    config_file('skip = ["a"]\n')

    # When
    code = _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert [item["name"] for item in doc["plugins"]] == ["b"]
    assert {"name": "a", "reason": "disabled"} in doc["skipped"]


def test_named_plugin_ignores_skip(
    patch_registry: Callable[..., None],
    config_file: Callable[[str], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify naming a plugin measures it even when the skip setting lists it."""
    # Given
    patch_registry(make_plugin(name="a"), make_plugin(name="b"))
    config_file('skip = ["a"]\n')

    # When
    code = _run("info", "a", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert [item["name"] for item in doc["plugins"]] == ["a"]


def test_config_error_exits_with_usage(
    patch_registry: Callable[..., None],
    config_file: Callable[[str], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify an unreadable config file exits 2 with a config_error document."""
    # Given
    patch_registry(make_plugin(name="a"))
    config_file("skip = [\n")

    # When
    code = _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.USAGE
    assert doc["error"]["code"] == "config_error"


def test_config_warning_stays_off_stdout_in_json_mode(
    patch_registry: Callable[..., None],
    config_file: Callable[[str], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a config warning goes to stderr so stdout stays one JSON document."""
    # Given
    patch_registry(make_plugin(name="a"))
    config_file("bogus = 1\n")

    # When
    code = _run("info", "--json")

    # Then
    captured = capsys.readouterr()
    assert code == ExitCode.OK
    assert json.loads(captured.out)["schema_version"] == 1
    assert "unknown key 'bogus'" in captured.err


def test_reason_text_covers_disabled() -> None:
    """Verify the disabled skip reason has a message naming the skip setting."""
    # Given / When
    text = cli._reason_text(SkipReason.DISABLED)

    # Then
    assert "skip setting" in text


def test_clean_json_reports_skipped_plugin_without_running_it(
    patch_registry: Callable[..., None],
    config_file: Callable[[str], None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify clean --json reports a skip-listed plugin as disabled and never runs it."""
    # Given
    patch_registry(
        make_plugin(name="a", actions=(make_action(name="a-act"),)),
        make_plugin(name="b", actions=(make_action(name="b-act"),)),
    )
    config_file('skip = ["a"]\n')
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--json", "--yes")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert {"name": "a", "reason": "disabled"} in doc["skipped"]
    assert [item["name"] for item in doc["plugins"]] == ["b"]
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["b-act"]


def test_measure_hands_each_plugin_its_configured_settings(mocker: MockerFixture) -> None:
    """Verify measuring passes every plugin the settings the run's config resolves for it."""
    # Given
    measure = mocker.patch("purger.cli.measure_plugin", autospec=True, side_effect=_fake_measure)
    plugin = make_plugin(
        name="alpha",
        settings=(
            Setting(name="depth", value_type=int, default=3, description="How deep to look."),
        ),
    )
    config = Config(values={"alpha": {"depth": 8}})

    # When
    cli._measure([plugin], dangerous=False, display=None, config=config)

    # Then
    settings = measure.call_args.kwargs["settings"]
    assert settings == config.settings_for(plugin)
    assert settings.get_integer("depth") == 8


def test_info_verbose_prints_size_breakdown_rows(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify info -v breaks each plugin down into its rows."""
    # Given
    patch_registry(make_plugin(name="alpha", actions=(make_action(name="prune"),)))

    # When
    code = _run("info", "-v")

    # Then
    assert code == ExitCode.OK
    assert "row-of-prune" in capsys.readouterr().out


def test_info_without_verbose_skips_size_breakdown_measurement(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify a plain info builds no size breakdown rows."""
    # Given
    plugin = make_plugin(name="alpha")
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={"alpha": plugin})
    measure = _patch_measure(mocker, side_effect=_fake_measure)

    # When
    _run("info")

    # Then
    assert measure.call_args.kwargs["with_details"] is False


def test_clean_dry_run_prints_one_summary_line_per_action(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify the dry run sums up a path action instead of listing every path."""
    # Given a path action that resolves three paths
    patch_registry(
        make_plugin(name="alpha", actions=(make_action(name="files", cleanup=DeletePaths("/x")),))
    )

    # When
    code = _run("clean", "--dry-run")

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert out.count("would delete 3 items") == 1
    assert "/fake/" not in out
    assert "row-of-files" not in out


def test_clean_dry_run_verbose_lists_rows(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify the dry run with -v shows each action's rows."""
    # Given
    patch_registry(
        make_plugin(name="alpha", actions=(make_action(name="files", cleanup=DeletePaths("/x")),))
    )

    # When
    _run("clean", "--dry-run", "-v")

    # Then
    assert capsys.readouterr().out.count("row-of-files") == 1


def test_clean_short_n_is_dry_run(
    patch_registry: Callable[..., None], mocker: MockerFixture
) -> None:
    """Verify -n plans without running anything."""
    # Given
    patch_registry(make_plugin(name="alpha", actions=(make_action(name="prune"),)))
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean", "-n")

    # Then
    assert code == ExitCode.OK
    run_action.assert_not_called()


def test_clean_verbose_shows_size_breakdown_before_prompts(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify a real clean -v shows the rows before asking anything."""
    # Given
    patch_registry(make_plugin(name="alpha", actions=(make_action(name="prune"),)))
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    seen: list[str] = []

    def answer(*args: object, **kwargs: object) -> bool:
        seen.append(capsys.readouterr().out)
        return False

    mocker.patch("purger.cli.Confirm.ask", autospec=True, side_effect=answer)

    # When
    _run("clean", "-v")

    # Then
    assert seen
    assert "row-of-prune" in seen[0]


def test_info_json_includes_details_without_verbose(
    patch_registry: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify agents get size breakdown rows without passing -v."""
    # Given
    patch_registry(make_plugin(name="alpha", actions=(make_action(name="prune"),)))

    # When
    _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert doc["plugins"][0]["actions"][0]["details"][0]["label"] == "row-of-prune"


def test_clean_verbose_skips_rows_of_actions_with_nothing_to_clean(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify clean -v lists only actions that have something to remove."""
    # Given one empty action and one with data
    plugin = make_plugin(
        name="alpha", actions=(make_action(name="empty"), make_action(name="full"))
    )
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={"alpha": plugin})

    def measure(
        plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings, with_details: bool
    ) -> PluginReport:
        actions = [
            ActionReport(
                action=action,
                size_bytes=0 if action.name == "empty" else _SIZE,
                resolved_paths=[],
                details=[SizeBreakdownRow(label=f"row-of-{action.name}", size_bytes=0)],
            )
            for action in plugin.actions
        ]
        return PluginReport(
            settings=EMPTY_SETTINGS, plugin=plugin, total_size_bytes=_SIZE, actions=actions
        )

    _patch_measure(mocker, side_effect=measure)
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    mocker.patch("purger.cli.Confirm.ask", autospec=True, return_value=False)

    # When
    _run("clean", "-v")

    # Then
    out = capsys.readouterr().out
    assert "row-of-full" in out
    assert "row-of-empty" not in out


@pytest.mark.parametrize("args", [("info", "-v"), ("clean", "-n", "-v")])
def test_verbose_replaces_table_with_sections(
    args: tuple[str, ...],
    patch_registry: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify -v prints a section per plugin and a total instead of the table."""
    # Given
    patch_registry(
        make_plugin(name="alpha", description="alpha tool", actions=(make_action(name="prune"),))
    )

    # When
    code = _run(*args)

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert "Cache" not in out
    assert "alpha tool" in out
    assert "row-of-prune" in out
    assert "Total" in out


def test_required_setting_set_empty_by_environment_is_not_configured(
    patch_registry: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify an empty environment value for a required setting skips the plugin."""
    # Given
    setting = Setting(
        name="paths",
        value_type=list[str],
        default=["/somewhere"],
        description="Folders.",
        required=True,
    )
    patch_registry(make_plugin(name="a", settings=(setting,)))
    monkeypatch.setenv("PURGER_A__PATHS", "")

    # When
    code = _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert doc["plugins"] == []
    assert {"name": "a", "reason": "not_configured"} in doc["skipped"]


def test_unavailable_plugin_is_not_reported_as_not_configured(
    patch_registry: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify availability is checked before configuration."""
    # Given
    setting = Setting(
        name="paths", value_type=list[str], default=(), description="Folders.", required=True
    )
    patch_registry(make_plugin(name="a", settings=(setting,), skip_unless=lambda: False))

    # When
    code = _run("info", "--json")

    # Then
    doc = _json(capsys)
    assert code == ExitCode.OK
    assert {"name": "a", "reason": "unavailable"} in doc["skipped"]


def test_each_command_starts_with_fresh_run_caches(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a second command in one process scans again instead of reusing the first scan."""
    # Given a plugin whose only action reads a counting once_per_run scan
    calls: list[PluginSettings] = []

    @once_per_run
    def scan(settings: PluginSettings) -> list[Path]:
        calls.append(settings)
        return []

    plugin = make_plugin(
        name="alpha",
        actions=(
            Action(
                name="found",
                risk_level=RiskLevel.SAFE,
                description="Remove found things.",
                cleanup=DeletePaths(found_by=scan),
            ),
        ),
    )
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={"alpha": plugin})

    # When
    _run("info", "--json")
    _run("info", "--json")
    capsys.readouterr()

    # Then
    assert len(calls) == 2
