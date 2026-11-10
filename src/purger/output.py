"""Render cache reports as a rich table for people or JSON documents for agents."""

import sys
from typing import Any

from nclutils.strings import human_size
from rich.markup import escape
from rich.table import Table

from purger.plugin import Tier
from purger.reports import ActionReport, PluginReport, SkippedPlugin, sum_known

SCHEMA_VERSION = 1

_TIER_STYLES = {Tier.SAFE: "green", Tier.CAUTION: "yellow", Tier.DANGEROUS: "red"}


def grand_total(reports: list[PluginReport]) -> int | None:
    """Sum the known plugin footprints so callers can report one overall figure.

    Args:
        reports: Plugin reports to total.

    Returns:
        The sum of known footprints, or None when no footprint is known.
    """
    return sum_known(r.footprint_bytes for r in reports)


def _size_text(size: int | None) -> str:
    return "-" if size is None else human_size(size)


def render_table(reports: list[PluginReport], *, show_status: bool = False) -> Table:
    """Build the human-readable table of plugins and their actions.

    Args:
        reports: Plugin reports to show.
        show_status: Add a Status column with each action's outcome.

    Returns:
        A rich table ready to print.
    """
    table = Table(show_footer=True, expand=False)
    table.add_column("Name", footer="Found in caches")
    table.add_column("Tier")
    table.add_column("Size", justify="right", footer=_size_text(grand_total(reports)))
    if show_status:
        table.add_column("Status")
    table.add_column("Description")

    blanks = [""] if show_status else []
    for report in reports:
        table.add_row(
            f"[bold]{escape(report.plugin.name)}[/bold]",
            "",
            f"[bold]{_size_text(report.footprint_bytes)}[/bold]",
            *blanks,
            escape(report.plugin.description),
            end_section=not report.actions,
        )
        for index, action in enumerate(report.actions):
            style = _TIER_STYLES[action.action.tier]
            status = [escape(str(action.status or ""))] if show_status else []
            table.add_row(
                f"  {escape(action.action.name)}",
                f"[{style}]{action.action.tier.value}[/{style}]",
                _size_text(action.size_bytes),
                *status,
                escape(action.action.help),
                end_section=index == len(report.actions) - 1,
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
