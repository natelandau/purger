"""Run approved cleanup actions and measure the space they freed."""

from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from nclutils import pp

from purger.concurrency import map_as_completed
from purger.registry import is_busy
from purger.reports import ActionReport, PluginReport, Status
from purger.runner import run_action
from purger.safety import invoking_home
from purger.sizing import measure_footprint


@dataclass
class Execution:
    """Outcome of running approved actions.

    Attributes:
        ran: Plugins with at least one action started, in plugin order.
        interrupted: Whether Ctrl-C stopped the run.
    """

    ran: list[PluginReport]
    interrupted: bool


def mark_unfinished(reports: Iterable[ActionReport]) -> None:
    """Mark every action without an outcome as skipped by an interrupt.

    Call this after Ctrl-C so no action is left without a status in the report.

    Args:
        reports: Action reports to mark.
    """
    for report in reports:
        if report.status is None:
            report.status = Status.SKIPPED
            report.error = "interrupted"


def execute(
    reports: list[PluginReport],
    approved: list[ActionReport],
    *,
    verbose: bool,
    interactive: bool,
) -> Execution:
    """Run approved actions in plugin order, announcing each one before it runs.

    A plugin whose tool is running when its turn comes is skipped, with its
    actions marked skipped. Ctrl-C stops the run: the action that was running is marked failed, since it
    may have changed things before it stopped, and the rest are marked skipped.

    Args:
        reports: All measured plugin reports, which set the run order.
        approved: The actions to run.
        verbose: Stream tool output.
        interactive: Whether a person is at the terminal.

    Returns:
        Execution: The plugins that ran and whether the run was interrupted.
    """
    try:
        home = invoking_home()
    except RuntimeError:
        # A relative home makes delete_path refuse every path, while commands still run.
        home = Path()
    pending = {id(report) for report in approved}
    ran: dict[int, PluginReport] = {}
    current: ActionReport | None = None
    interrupted = False
    try:
        for plugin_report in reports:
            todo = [report for report in plugin_report.actions if id(report) in pending]
            if not todo:
                continue
            # The tool may have started while purger waited at a confirmation prompt.
            if is_busy(plugin_report.plugin):
                pp.warning(f"Skipped {plugin_report.plugin.name}: its tool is running now")
                for report in todo:
                    report.status = Status.SKIPPED
                    report.error = "busy"
                continue
            for report in todo:
                label = f"{plugin_report.plugin.name}/{report.action.name}"
                if report.action.needs_sudo:
                    pp.warning(f"Running {label} with sudo")
                else:
                    pp.info(f"Running {label}")
                ran[id(plugin_report)] = plugin_report
                current = report
                run_action(report, home=home, verbose=verbose, interactive=interactive)
    except KeyboardInterrupt:
        if current is not None and current.status is None:
            current.status = Status.FAILED
            current.error = "interrupted"
        mark_unfinished(approved)
        interrupted = True
    return Execution(ran=list(ran.values()), interrupted=interrupted)


def _set_freed(ran: list[PluginReport], *, dangerous: bool) -> None:
    def measure(plugin_report: PluginReport) -> int | None:
        return measure_footprint(
            plugin_report.plugin,
            include_dangerous=dangerous,
            prepared_paths=plugin_report.prepared_paths,
        )

    for index, after in map_as_completed(measure, items=ran):
        plugin_report = ran[index]
        before = plugin_report.footprint_bytes
        plugin_report.after_bytes = after
        plugin_report.freed_bytes = (
            None if before is None or after is None else max(before - after, 0)
        )


def record_freed(ran: list[PluginReport], *, dangerous: bool, show_progress: bool) -> bool:
    """Re-measure plugins that ran so the space freed can be reported.

    Args:
        ran: Plugins with at least one action started.
        dangerous: Measure DANGEROUS actions too, matching the first measurement.
        show_progress: Show a spinner while measuring.

    Returns:
        bool: True when the user interrupted the measurement.
    """
    if not ran:
        return False
    progress = pp.step("Measuring freed space", ephemeral=True) if show_progress else nullcontext()
    try:
        with progress:
            _set_freed(ran, dangerous=dangerous)
    except KeyboardInterrupt:
        for plugin_report in ran:
            plugin_report.freed_bytes = None
            plugin_report.after_bytes = None
        return True
    return False
