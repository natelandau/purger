"""Tests for the live per-plugin measuring display."""

import io

import pytest
from rich.console import Console

from purger.measure_display import MeasureDisplay
from purger.reports import PluginReport
from tests.conftest import EMPTY_SETTINGS, make_plugin


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=True, width=80), buffer


def _report(total_size_bytes: int | None) -> PluginReport:
    return PluginReport(
        settings=EMPTY_SETTINGS,
        plugin=make_plugin(name="uv"),
        total_size_bytes=total_size_bytes,
        actions=[],
    )


def _status(display: MeasureDisplay, name: str) -> str:
    task = next(t for t in display._progress.tasks if t.fields["name"].strip() == name)
    return task.fields["status"]


def test_measure_display_rows_start_measuring() -> None:
    """Verify every plugin gets its own row that starts out measuring."""
    # Given a display for two plugins
    console, _ = _console()

    # When it is created
    display = MeasureDisplay(["uv", "docker"], console=console)

    # Then each plugin has an unfinished row marked as measuring
    assert len(display._progress.tasks) == 2
    assert all(not task.finished for task in display._progress.tasks)
    assert "measuring" in _status(display, "uv")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            _report(3_000_000),
            "3.0 MB",
        ),
        (
            _report(None),
            "size unknown",
        ),
    ],
)
def test_measure_display_finish_shows_result(result: PluginReport, expected: str) -> None:
    """Verify a finished row shows the plugin's total size."""
    # Given a display with one plugin row
    console, _ = _console()
    display = MeasureDisplay(["uv"], console=console)

    # When the plugin finishes
    display.finish("uv", result)

    # Then the row is complete and shows the result
    assert display._progress.tasks[0].finished
    assert expected in _status(display, "uv")


def test_measure_display_is_transient_and_stops() -> None:
    """Verify the display clears itself and stops its live refresh on exit."""
    # Given a display that runs to completion
    console, _ = _console()

    # When it is used as a context manager
    with MeasureDisplay(["uv"], console=console) as display:
        display.finish(
            "uv",
            _report(1),
        )

    # Then it is transient and no longer refreshing
    assert display._progress.live.transient
    assert not display._progress.live.is_started


def test_measure_display_silent_without_terminal() -> None:
    """Verify the display writes nothing when the console is not a terminal."""
    # Given a console that writes to a plain buffer, not a terminal
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False)

    # When a full measuring pass runs
    with MeasureDisplay(["uv"], console=console) as display:
        display.finish(
            "uv",
            _report(1),
        )

    # Then nothing was written
    assert buffer.getvalue() == ""
