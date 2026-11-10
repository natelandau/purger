"""Tests for running approved actions and measuring freed space."""

from pathlib import Path

from pytest_mock import MockerFixture

from purger.execution import execute, mark_unfinished, record_freed
from purger.plugin import Plugin
from purger.reports import ActionReport, PluginReport, Status
from tests.conftest import make_action, make_plugin


def _plugin_report(name: str, *action_names: str, footprint: int | None = 100) -> PluginReport:
    actions = [
        ActionReport(action=make_action(name=action), size_bytes=10, resolved_paths=[])
        for action in action_names
    ]
    plugin = make_plugin(name=name, actions=tuple(r.action for r in actions))
    return PluginReport(plugin=plugin, footprint_bytes=footprint, actions=actions)


def _mark_done(report: ActionReport, *, home: Path, verbose: bool, interactive: bool) -> None:
    report.status = Status.DONE


def test_execute_runs_only_approved_in_plugin_order(mocker: MockerFixture) -> None:
    """Verify execute runs approved actions in plugin order and returns the plugins that ran."""
    # Given two plugins with one approved action each, approved out of order
    one = _plugin_report("one", "a", "b")
    two = _plugin_report("two", "c")
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    result = execute([one, two], [two.actions[0], one.actions[1]], verbose=True, interactive=False)

    # Then
    assert [c.args[0].action.name for c in run_action.call_args_list] == ["b", "c"]
    assert run_action.call_args.kwargs["verbose"] is True
    assert run_action.call_args.kwargs["interactive"] is False
    assert result.ran == [one, two]
    assert result.interrupted is False
    assert one.actions[0].status is None


def test_execute_interrupt_fails_current_and_skips_rest(mocker: MockerFixture) -> None:
    """Verify Ctrl-C marks the running action failed and the untouched ones skipped."""
    # Given
    report = _plugin_report("one", "a", "b")
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=KeyboardInterrupt)

    # When
    result = execute([report], list(report.actions), verbose=False, interactive=False)

    # Then
    assert result.interrupted is True
    assert result.ran == [report]
    assert [(r.status, r.error) for r in report.actions] == [
        (Status.FAILED, "interrupted"),
        (Status.SKIPPED, "interrupted"),
    ]


def test_execute_announces_sudo(mocker: MockerFixture) -> None:
    """Verify a sudo action is announced as a warning before it runs."""
    # Given
    action = make_action(name="rooty", command=("tool",), needs_sudo=True)
    report = PluginReport(
        plugin=make_plugin(name="one", actions=(action,)),
        footprint_bytes=1,
        actions=[ActionReport(action=action, size_bytes=1, resolved_paths=[])],
    )
    mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)
    warning = mocker.patch("purger.execution.pp.warning", autospec=True)

    # When
    execute([report], list(report.actions), verbose=False, interactive=True)

    # Then
    warning.assert_called_once_with("Running one/rooty with sudo")


def test_mark_unfinished_leaves_finished_actions() -> None:
    """Verify only actions without a status are marked skipped."""
    # Given
    report = _plugin_report("one", "a", "b")
    report.actions[0].status = Status.DONE

    # When
    mark_unfinished(report.actions)

    # Then
    assert [r.status for r in report.actions] == [Status.DONE, Status.SKIPPED]
    assert report.actions[0].error is None


def test_record_freed_sets_difference_and_clamps(mocker: MockerFixture) -> None:
    """Verify freed bytes are before minus after, never negative, and None when unknown."""
    # Given three plugins: shrank, grew, and unknown afterwards
    shrank = _plugin_report("shrank", "a", footprint=100)
    grew = _plugin_report("grew", "a", footprint=100)
    unknown = _plugin_report("unknown", "a", footprint=100)
    after = {"shrank": 40, "grew": 150, "unknown": None}
    mocker.patch(
        "purger.execution.measure_footprint",
        autospec=True,
        side_effect=lambda plugin, *, include_dangerous: after[plugin.name],
    )

    # When
    interrupted = record_freed([shrank, grew, unknown], dangerous=False, show_progress=False)

    # Then
    assert interrupted is False
    assert [r.freed_bytes for r in (shrank, grew, unknown)] == [60, 0, None]
    assert [r.after_bytes for r in (shrank, grew, unknown)] == [40, 150, None]


def test_record_freed_interrupt_clears_freed(mocker: MockerFixture) -> None:
    """Verify Ctrl-C while re-measuring reports no freed space and returns True."""
    # Given
    report = _plugin_report("one", "a")
    mocker.patch("purger.execution.measure_footprint", autospec=True, side_effect=KeyboardInterrupt)

    # When
    interrupted = record_freed([report], dangerous=True, show_progress=False)

    # Then
    assert interrupted is True
    assert report.freed_bytes is None
    assert report.after_bytes is None


def test_record_freed_interrupt_clears_sizes_already_measured(mocker: MockerFixture) -> None:
    """Verify Ctrl-C clears after sizes recorded before the interrupt, so no half result shows."""
    # Given one plugin measured before the interrupt
    first = _plugin_report("first", "a", footprint=100)
    second = _plugin_report("second", "a", footprint=100)

    def measure(plugin: Plugin, *, include_dangerous: bool) -> int:
        if plugin.name == "second":
            raise KeyboardInterrupt
        return 40

    mocker.patch("purger.execution.measure_footprint", autospec=True, side_effect=measure)

    # When
    interrupted = record_freed([first, second], dangerous=False, show_progress=False)

    # Then
    assert interrupted is True
    assert [r.after_bytes for r in (first, second)] == [None, None]
    assert [r.freed_bytes for r in (first, second)] == [None, None]


def test_record_freed_nothing_ran_measures_nothing(mocker: MockerFixture) -> None:
    """Verify no measurement happens when no plugin ran."""
    # Given
    measure = mocker.patch("purger.execution.measure_footprint", autospec=True)

    # When
    interrupted = record_freed([], dangerous=False, show_progress=True)

    # Then
    assert interrupted is False
    measure.assert_not_called()


def test_execute_without_home_refuses_path_deletion(mocker: MockerFixture) -> None:
    """Verify execute still runs when no home is known, passing a home that delete_path refuses."""
    # Given no home directory can be determined
    mocker.patch("purger.execution.invoking_home", autospec=True, side_effect=RuntimeError)
    one = _plugin_report("one", "a")
    run_action = mocker.patch("purger.execution.run_action", autospec=True, side_effect=_mark_done)

    # When
    execute([one], one.actions, verbose=False, interactive=False)

    # Then
    assert not run_action.call_args.kwargs["home"].is_absolute()
