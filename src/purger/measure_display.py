"""Live per-plugin progress shown while caches are measured."""

from collections.abc import Sequence
from types import TracebackType
from typing import Self

from nclutils import pp
from nclutils.strings import human_size
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn

from purger.reports import PluginReport


class MeasureDisplay:
    """Show one live row per plugin so slow measurements are visible while others finish.

    Each row shows a spinner and a pulsing bar until its plugin is measured, then a full
    bar with the plugin's total size. The bar pulses rather than fills because tool
    size commands report no progress. The display clears itself on exit because the
    results table printed afterward carries the same numbers.
    """

    def __init__(self, names: Sequence[str], *, console: Console | None = None) -> None:
        console = console or pp.err_console()
        self._progress = Progress(
            SpinnerColumn(finished_text="[green]✓[/]"),
            TextColumn("[bold]{task.fields[name]}[/]"),
            BarColumn(bar_width=24),
            TextColumn("{task.fields[status]}"),
            console=console,
            transient=True,
            # Rows that cannot redraw in place would only add noise to a pipe or log
            disable=not console.is_interactive,
        )
        width = max((len(name) for name in names), default=0)
        self._tasks: dict[str, TaskID] = {
            name: self._progress.add_task(
                "", total=None, name=escape(name.ljust(width)), status="[dim]measuring[/]"
            )
            for name in names
        }

    def __enter__(self) -> Self:
        """Start the live display."""
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the live display, clearing its rows."""
        self._progress.stop()

    def finish(self, name: str, result: PluginReport) -> None:
        """Mark a plugin's row done and show its total size.

        Args:
            name: The plugin name the row was created for.
            result: The plugin's measurement.
        """
        if result.total_size_bytes is None:
            status = "[dim]size unknown[/]"
        else:
            status = human_size(result.total_size_bytes)
        self._progress.update(self._tasks[name], total=1, completed=1, status=status)
