"""Render cache reports as a rich table for people or JSON documents for agents."""

import shlex
import sys
from typing import Any

from nclutils.strings import human_size
from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from purger.details import largest_first, merge
from purger.plugin import Detail
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
    for report in sorted(ran, key=lambda r: largest_first(r.freed_bytes, r.plugin.name)):
        table.add_row(
            f"[bold]{escape(report.plugin.name)}[/bold]",
            _size_text(report.footprint_bytes),
            _size_text(report.after_bytes),
            _size_text(report.freed_bytes),
        )
    return table


# Longest label at `-v`, so a row fits on one 80-column line next to its size and items.
_LABEL_WIDTH = 40
_ELLIPSIS = "\u2026"


def _shorten(text: str, width: int = _LABEL_WIDTH) -> str:
    """Cut the middle out of a long label, since both its start and end tell items apart.

    Args:
        text: The label.
        width: The longest result.

    Returns:
        str: The label, or its first and last characters around an ellipsis.
    """
    if len(text) <= width:
        return text
    head = (width - 1) // 2
    tail = width - 1 - head
    return f"{text[:head]}{_ELLIPSIS}{text[-tail:]}"


# Room kept for the items column when a long label must fold to fit the line.
_MIN_ITEMS_WIDTH = 20
# 5 = 2 leading spaces + a 2-cell connector + 1 space, the layout `pp.info(details=...)` uses.
_BRANCH_WIDTH = 5
_TEE, _CORNER, _PIPE = "\u251c\u2500", "\u2514\u2500", "\u2502 "


def _branch(
    console: Console,
    options: ConsoleOptions,
    renderable: RenderableType,
    *,
    lead: Text,
    last: bool,
) -> RenderResult:
    """Draw one tree branch: a connector on the first line, a guide on the lines after it.

    Uses the glyphs and `sub.pipe` style of `pp.info(details=...)`, which draws its
    tree only at a fixed offset from the margin and cannot nest it.
    """
    head, cont = (_CORNER, "  ") if last else (_TEE, _PIPE)
    inner = options.update(width=max(1, options.max_width - len(lead) - _BRANCH_WIDTH))
    for index, segments in enumerate(console.render_lines(renderable, inner, pad=False)):
        line = lead.copy()
        line.append(f"  {head if index == 0 else cont}", style="sub.pipe")
        line.append(" ")
        for segment in segments:
            line.append(segment.text, style=segment.style or "")
        yield line


class _RowTree:
    """Hang rows from the line above with tree connectors, and paths from each row at `-vv`.

    Each row is its own grid, so the columns must share widths to line up. The
    label width is worked out when the tree is drawn, from the width it gets, so
    on a narrow terminal the label folds and the size is never cut.
    """

    def __init__(self, lines: list[tuple[str, str, str, list[str], bool]]) -> None:
        self._lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        size_width = max(len(size) for _, size, _, _, _ in self._lines)
        items_width = min(max(len(items) for _, _, items, _, _ in self._lines), _MIN_ITEMS_WIDTH)
        # 4 = the two 2-cell gaps between the three columns.
        room = options.max_width - _BRANCH_WIDTH - size_width - items_width - 4
        label_width = max(1, min(max(len(label) for label, _, _, _, _ in self._lines), room))
        last_row = len(self._lines) - 1
        for row_index, (label, size, items, paths, dim) in enumerate(self._lines):
            grid = Table.grid(padding=(0, 2))
            grid.add_column(width=label_width, overflow="fold")
            grid.add_column(width=size_width, justify="right", no_wrap=True)
            grid.add_column(overflow="fold")
            style = "dim" if dim else ""
            grid.add_row(Text(label, style=style), Text(size, style=style), Text(items))
            row_last = row_index == last_row
            yield from _branch(console, options, grid, lead=Text(), last=row_last)
            # A path's lead keeps the row's own guide running while later rows follow.
            lead = Text.assemble("  ", (" " if row_last else _PIPE.strip(), "sub.pipe"))
            for path_index, path in enumerate(paths):
                yield from _branch(
                    console,
                    options,
                    Text(path, style="dim"),
                    lead=lead,
                    last=path_index == len(paths) - 1,
                )


def _detail_rows(
    rows: list[Detail], *, verbosity: int, trailer: tuple[str, str] | None = None
) -> _RowTree:
    """Prepare every row for display, with its paths only at `-vv`.

    Args:
        rows: The rows, largest first.
        verbosity: 1 shortens labels and shows no paths, and 2 or more shows
            full labels and full paths.
        trailer: A closing label and size, such as the remainder line, with no paths.

    Returns:
        _RowTree: The rows, ready to draw.
    """
    full = verbosity >= 2  # noqa: PLR2004
    lines = [
        (
            row.label if full else _shorten(row.label),
            _size_text(row.size_bytes),
            ", ".join(row.items),
            [str(path) for path in row.paths] if full else [],
            False,
        )
        for row in rows
    ]
    if trailer is not None:
        lines.append((trailer[0], trailer[1], "", [], True))
    return _RowTree(lines)


def _indented(block: RenderableType) -> Padding:
    """Indent a whole block, so wrapped lines stay inside it instead of at the margin."""
    return Padding(block, (0, 0, 0, 2))


def _remainder(report: PluginReport, rows: list[Detail]) -> tuple[str, int] | None:
    """Name and size the part of the footprint that no row covers, so the section adds up."""
    listed = sum_known(row.size_bytes for row in rows)
    if report.footprint_bytes is None or listed is None or listed >= report.footprint_bytes:
        return None
    # An action without rows that may free data, even an unknown amount, could cover the gap.
    unlisted = any(action.size_bytes != 0 and not action.details for action in report.actions)
    return ("not listed" if unlisted else "not cleaned", report.footprint_bytes - listed)


def _header(report: PluginReport) -> str:
    """Name a plugin's section with what the table row showed: size, description, partial mark."""
    partial = "  [dim](partial purge)[/dim]" if is_partial(report) else ""
    return (
        f"[bold]{escape(report.plugin.name)}[/bold]  {_size_text(report.footprint_bytes)}"
        f"  {escape(report.plugin.description)}{partial}"
    )


def _merged_rows(report: PluginReport, *, verbosity: int) -> RenderableType | None:
    """Merge every action's rows, so one project shows once, and end with any remainder."""
    rows = merge(row for action in report.actions for row in action.details)
    if not rows:
        return None
    remainder = _remainder(report, rows)
    trailer = None if remainder is None else (remainder[0], _size_text(remainder[1]))
    return _detail_rows(rows, verbosity=verbosity, trailer=trailer)


def _rows_by_action(report: PluginReport, *, verbosity: int) -> list[RenderableType]:
    """List each action under its name and tier, with its rows, since `clean` confirms by tier.

    An action without rows, such as a tool command, still gets its line, so every
    action a prompt may offer shows before it.
    """
    parts: list[RenderableType] = []
    for action in report.actions:
        # An empty action ends as nothing_to_clean, so listing it would only add noise.
        if action.size_bytes == 0:
            continue
        parts.append(
            Padding(
                f"{escape(action.action.name)} ({action.action.tier.value})"
                f"  {_size_text(action.size_bytes)}",
                (0, 0, 0, 2),
            )
        )
        if action.details:
            parts.append(_indented(_detail_rows(action.details, verbosity=verbosity)))
    return parts


def render_plugin_details(
    report: PluginReport, *, verbosity: int, by_action: bool = False
) -> RenderableType:
    """Build one plugin's `-v` section, which takes the place of its table row.

    Args:
        report: The measured plugin.
        verbosity: 1 shortens long labels, and 2 or more shows full labels and paths.
        by_action: Group rows under each action, as `clean` does, instead of
            merging them across actions, as `info` does.

    Returns:
        RenderableType: The header, followed by the rows when there are any.
    """
    parts: list[RenderableType] = [_header(report)]
    if by_action:
        parts.extend(_rows_by_action(report, verbosity=verbosity))
    elif (rows := _merged_rows(report, verbosity=verbosity)) is not None:
        parts.append(rows)
    return Group(*parts)


def render_total(reports: list[PluginReport]) -> str:
    """Build the line that closes the `-v` sections with the table's total.

    Args:
        reports: Plugin reports to total.

    Returns:
        str: The total line.
    """
    return f"[bold]Total[/bold]  {_size_text(grand_total(reports))}"


def action_summary(report: ActionReport) -> str:
    """Describe in one line what an action would do, for the dry-run plan.

    Args:
        report: The measured action.

    Returns:
        str: The item count and size for a path action, or the command it runs.
    """
    action = report.action
    if action.command is not None:
        sudo = "sudo " if action.needs_sudo else ""
        return f"would run {sudo}{shlex.join(action.command)}"
    count = len(report.resolved_paths)
    noun = "item" if count == 1 else "items"
    return f"would delete {count} {noun}, {_size_text(report.size_bytes)}"


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
        "details": [
            {
                "label": row.label,
                "size_bytes": row.size_bytes,
                "items": list(row.items),
                "paths": [str(path) for path in row.paths],
            }
            for row in report.details
        ],
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
