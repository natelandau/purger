"""Tests for the purger command line."""

import json
import sys
import threading
from collections.abc import Callable

import pytest
from pytest_mock import MockerFixture, MockType

from purger import cli
from purger.cli import ExitCode, app, confirm, main
from purger.measure_display import MeasureDisplay
from purger.plugin import Plugin, Tier
from purger.reports import ActionReport, PluginReport, SkipReason, Status
from tests.conftest import make_action, make_plugin, table_row

_SIZE = 100


def _fake_measure(plugin: Plugin, *, include_dangerous: bool) -> PluginReport:
    """Measure every action as a fixed size, honoring the DANGEROUS filter."""
    actions = [
        ActionReport(action=action, size_bytes=_SIZE, resolved_paths=[])
        for action in plugin.actions
        if include_dangerous or action.tier is not Tier.DANGEROUS
    ]
    return PluginReport(plugin=plugin, footprint_bytes=_SIZE * len(actions), actions=actions)


def _mark_done(report: ActionReport, *, home: object, verbose: bool, interactive: bool) -> None:
    report.status = Status.DONE


def _patch_measure(mocker: MockerFixture, *, side_effect: object) -> MockType:
    """Patch measuring in the CLI and re-measuring in execution with one shared mock."""
    measure = mocker.patch("purger.cli.measure_plugin", autospec=True, side_effect=side_effect)
    mocker.patch(
        "purger.execution.measure_footprint",
        autospec=True,
        side_effect=lambda plugin, *, include_dangerous: (
            measure(plugin, include_dangerous=include_dangerous).footprint_bytes
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
    patch_registry(make_plugin(name="macish", platforms=frozenset({"darwin"})))

    # When
    code = _run("info", "macish", "--json")

    # Then
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert code == ExitCode.OK
    assert doc["skipped"] == [{"name": "macish", "reason": "platform"}]
    assert "macish" in captured.err
    assert "linux" in captured.err


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
                make_action(name="safe-one", tier=Tier.SAFE),
                make_action(name="risky", tier=Tier.CAUTION),
                make_action(name="wipe", tier=Tier.DANGEROUS),
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
                make_action(name="prune", command=("tool", "prune")),
                make_action(name="risky", tier=Tier.CAUTION, command=("tool", "nuke")),
            ),
        )
    )
    run_action = mocker.patch("purger.execution.run_action", autospec=True)
    ask = mocker.patch("purger.cli.choose_one_from_list", autospec=True)

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
                make_action(name="sudo-safe", command=("tool", "x"), needs_sudo=True),
                make_action(name="risky", tier=Tier.CAUTION),
            ),
        )
    )
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    ask = mocker.patch("purger.cli.choose_one_from_list", autospec=True, return_value="no")
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--yes")

    # Then
    err = capsys.readouterr().err
    assert code == ExitCode.OK
    assert ask.call_count == 1
    assert ask.call_args.args[1].startswith("alpha (about ")
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["sudo-safe"]
    assert "alpha/sudo-safe" in err
    assert "sudo" in err


def test_clean_interrupt_at_prompt_runs_nothing(
    patch_registry: Callable[..., None],
    mocker: MockerFixture,
) -> None:
    """Verify Ctrl-C at a prompt, which the prompt reports as None, exits 130 without running anything."""
    # Given a prompt cancelled with Ctrl-C
    patch_registry(make_plugin(name="alpha"))
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    mocker.patch("purger.cli.choose_one_from_list", autospec=True, return_value=None)
    run_action = mocker.patch("purger.execution.run_action", autospec=True)

    # When
    code = _run("clean")

    # Then
    assert code == ExitCode.INTERRUPTED
    run_action.assert_not_called()


@pytest.mark.parametrize(("answer", "expected"), [("no", False), ("yes", True)])
def test_confirm_maps_answers(answer: str, expected: bool, mocker: MockerFixture) -> None:
    """Verify confirm returns True only for Yes."""
    # Given
    ask = mocker.patch("purger.cli.choose_one_from_list", autospec=True, return_value=answer)

    # When
    result = confirm("Proceed?")

    # Then
    assert result is expected
    assert ask.call_args.args[0] == [("Yes", "yes"), ("No", "no")]


def test_confirm_cancelled_prompt_is_interrupt(mocker: MockerFixture) -> None:
    """Verify a cancelled prompt raises KeyboardInterrupt so Ctrl-C stops the run."""
    # Given a prompt that reports cancellation as None
    mocker.patch("purger.cli.choose_one_from_list", autospec=True, return_value=None)

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
    before = _fake_measure(plugin, include_dangerous=False)
    after = PluginReport(plugin=plugin, footprint_bytes=40, actions=[])
    _patch_measure(mocker, side_effect=[before, after])
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    code = _run("clean", "--yes")

    # Then
    out = capsys.readouterr().out
    assert code == ExitCode.OK
    for header in ("Before", "After", "Freed"):
        assert header in out
    results = out[out.index("Before") :]
    assert table_row(results, "alpha") == ["alpha", "100 B", "40 B", "60 B"]
    assert "Freed 60 B" not in out
    assert "Status" not in out


def test_clean_human_lists_failures_under_table(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a tool with one success and one failure gets a results row and a failure line."""
    # Given a tool whose second action fails
    plugin = make_plugin(name="alpha", actions=(make_action(name="one"), make_action(name="two")))
    mocker.patch("purger.cli.discover_plugins", autospec=True, return_value={plugin.name: plugin})
    before = _fake_measure(plugin, include_dangerous=False)
    after = PluginReport(plugin=plugin, footprint_bytes=50, actions=[])
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
                make_action(name="rooty", command=("tool", "x"), needs_sudo=True),
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
    before = _fake_measure(plugin, include_dangerous=False)
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
        make_plugin(name="alpha", actions=(make_action(name="risky", tier=Tier.CAUTION),))
    )
    mocker.patch.object(cli.sys.stdin, "isatty", return_value=True)
    mocker.patch("purger.cli.choose_one_from_list", autospec=True, return_value="yes")
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
                make_action(name="risky", tier=Tier.CAUTION),
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
    """Verify concurrent measuring reports plugins and skips in plugin order."""
    # Given alpha finishes only after beta starts, and gamma does not apply here
    beta_started = threading.Event()

    def measure(plugin: Plugin, *, include_dangerous: bool) -> PluginReport:
        if plugin.name == "alpha":
            assert beta_started.wait(timeout=5)
        elif plugin.name == "beta":
            beta_started.set()
        return _fake_measure(plugin, include_dangerous=include_dangerous)

    mocker.patch("purger.cli.measure_plugin", autospec=True, side_effect=measure)
    mocker.patch(
        "purger.cli.skip_reason",
        autospec=True,
        side_effect=lambda plugin: SkipReason.PLATFORM if plugin.name == "gamma" else None,
    )
    plugins = [make_plugin(name=name) for name in ("alpha", "beta", "gamma", "delta")]
    display = mocker.Mock(spec=MeasureDisplay)

    # When
    scan = cli._measure(plugins, dangerous=False, display=display)

    # Then
    assert [r.plugin.name for r in scan.reports] == ["alpha", "beta", "delta"]
    assert [s.name for s in scan.skipped] == ["gamma"]
    finished = {call.kwargs["name"] for call in display.finish.call_args_list}
    assert finished == {"alpha", "beta", "gamma", "delta"}
