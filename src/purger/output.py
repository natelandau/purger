"""Render cache reports as a rich table for people or JSON documents for agents."""

import sys
from typing import Any

from nclutils.strings import human_size
from rich import box
from rich.markup import escape
from rich.table import Table

from purger.reports import ActionReport, PluginReport, SkippedPlugin, sum_known

SCHEMA_VERSION = 1

_CHECK = "\u2713"


def grand_total(reports: list[PluginReport]) -> int | None:
    """Sum the known plugin footprints so callers can report one overall figure.

    Args:
        reports: Plugin reports to total.

    Returns:
        The sum of known footprints, or None when no footprint is known.
    """
    return sum_known(r.footprint_bytes for r in reports)


def _size_text(size: int | None) -> str:
    return "?" if size is None else human_size(size)


def is_partial(report: PluginReport) -> bool:
    """Report whether a clean would free only part of a tool's measured size.

    A tool is exact when every included action has a known size, or when the
    known sizes cover the whole footprint, such as a full-wipe action under
    `--dangerous`, or when there is nothing to free. The report holds only the
    actions this run includes.

    Args:
        report: The measured plugin.

    Returns:
        bool: True when the size shown is more than a clean is known to free.
    """
    sizes = [action.size_bytes for action in report.actions]
    if None not in sizes:
        return False
    # An unknown size outside the footprint means a zero footprint may still hide data.
    unknown_outside = any(
        action.size_bytes is None and action.action.outside_footprint for action in report.actions
    )
    if report.footprint_bytes == 0 and not unknown_outside:
        return False
    known = sum_known(sizes)
    if report.footprint_bytes is None or known is None:
        return True
    return known < report.footprint_bytes


def _largest_first(size: int | None, name: str) -> tuple[bool, int, str]:
    """Sort key putting the largest size first, unknown sizes last, and ties by name."""
    return (size is None, -(size or 0), name)


def _table() -> Table:
    return Table(
        show_footer=True, expand=False, box=box.SIMPLE, show_edge=False, footer_style="bold"
    )


def render_table(reports: list[PluginReport]) -> Table:
    """Build the table of tools and their cache sizes, sorted by name.

    Action names and tiers stay out of this table so users never need to know
    which commands purger runs inside each tool.

    Args:
        reports: Plugin reports to show.

    Returns:
        A rich table ready to print.
    """
    rows = sorted(
        ((report, is_partial(report)) for report in reports),
        key=lambda row: row[0].plugin.name.casefold(),
    )
    any_partial = any(partial for _, partial in rows)
    table = _table()
    table.add_column("Cache", footer="Total")
    table.add_column("Size", justify="right", footer=_size_text(grand_total(reports)))
    table.add_column("Partial\npurge", justify="center", footer=_CHECK if any_partial else "")
    table.add_column("Description")
    for report, partial in rows:
        table.add_row(
            f"[bold]{escape(report.plugin.name)}[/bold]",
            _size_text(report.footprint_bytes),
            _CHECK if partial else "",
            escape(report.plugin.description),
        )
    return table


def render_results(ran: list[PluginReport]) -> Table:
    """Build the table of each tool's cache size before and after a clean.

    Args:
        ran: Plugin reports for the tools where at least one action ran.

    Returns:
        A rich table ready to print.
    """
    # Freed is known only when both sizes are, so these totals agree with each other.
    measured = [r for r in ran if r.freed_bytes is not None]
    table = _table()
    table.add_column("Cache", footer="Total")
    for title, values in (
        ("Before", [r.footprint_bytes for r in measured]),
        ("After", [r.after_bytes for r in measured]),
        ("Freed", [r.freed_bytes for r in measured]),
    ):
        table.add_column(title, justify="right", footer=_size_text(sum_known(values)))
    for report in sorted(ran, key=lambda r: _largest_first(r.freed_bytes, r.plugin.name)):
        table.add_row(
            f"[bold]{escape(report.plugin.name)}[/bold]",
            _size_text(report.footprint_bytes),
            _size_text(report.after_bytes),
            _size_text(report.freed_bytes),
        )
    return table


def _action_dict(report: ActionReport) -> dict[str, Any]:
    action = report.action
    return {
        "name": action.name,
        "tier": action.tier.value,
        "help": action.help,
        "size_bytes": report.size_bytes,
        "command": list(action.command) if action.command is not None else None,
        "paths": [str(p) for p in report.resolved_paths],
        "needs_sudo": action.needs_sudo,
    }


def _document(
    reports: list[PluginReport],
    skipped: list[SkippedPlugin],
    *,
    include_results: bool,
) -> dict[str, Any]:
    plugins = []
    for report in reports:
        actions = []
        for action_report in report.actions:
            data = _action_dict(action_report)
            if include_results:
                data["status"] = action_report.status.value if action_report.status else None
                data["error"] = action_report.error
            actions.append(data)
        plugin: dict[str, Any] = {
            "name": report.plugin.name,
            "description": report.plugin.description,
            "footprint_bytes": report.footprint_bytes,
            "actions": actions,
        }
        if include_results:
            plugin["freed_bytes"] = report.freed_bytes
        plugins.append(plugin)

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform": sys.platform,
        "total_bytes": grand_total(reports),
        "plugins": plugins,
        "skipped": [{"name": s.name, "reason": s.reason.value} for s in skipped],
    }
    if include_results:
        document["total_freed_bytes"] = sum_known(r.freed_bytes for r in reports)
    return document


def info_document(reports: list[PluginReport], skipped: list[SkippedPlugin]) -> dict[str, Any]:
    """Build the JSON-ready document for `info --json`.

    Args:
        reports: Measured plugin reports.
        skipped: Plugins left out of the run.

    Returns:
        A JSON-serializable dict with a stable shape.
    """
    return _document(reports=reports, skipped=skipped, include_results=False)


def clean_document(reports: list[PluginReport], skipped: list[SkippedPlugin]) -> dict[str, Any]:
    """Build the JSON-ready document for `clean --json`, adding outcomes and freed bytes.

    Args:
        reports: Plugin reports after the cleanup ran.
        skipped: Plugins left out of the run.

    Returns:
        A JSON-serializable dict extending the info document.
    """
    return _document(reports=reports, skipped=skipped, include_results=True)


def error_document(code: str, message: str) -> dict[str, Any]:
    """Build the JSON error document so agents can parse failures.

    Args:
        code: Stable machine-readable error code.
        message: Human-readable explanation.

    Returns:
        A JSON-serializable dict.
    """
    return {"schema_version": SCHEMA_VERSION, "error": {"code": code, "message": message}}
