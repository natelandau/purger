"""Build the detail rows that break an action's size down for a person to inspect."""

import logging
from collections.abc import Iterable
from pathlib import Path

from purger.config import PluginSettings
from purger.plugin import Action, Detail, PathSizes
from purger.reports import sum_known
from purger.safety import invoking_home

logger = logging.getLogger(__name__)


def tilde(path: Path) -> str:
    """Show a path with the invoking user's home folder as `~`, so labels stay short.

    Args:
        path: The path to show.

    Returns:
        str: The path, starting with `~` when it is inside the home folder.
    """
    try:
        home = invoking_home()
    except RuntimeError:
        return str(path)
    # Callers pass paths under either form, since some build them from the resolved home.
    for base in (home, home.resolve()):
        if path == base:
            return "~"
        if path.is_relative_to(base):
            return f"~/{path.relative_to(base)}"
    return str(path)


def largest_first(size: int | None, name: str) -> tuple[bool, int, str]:
    """Sort key putting the largest size first, unknown sizes last, and ties by name."""
    return (size is None, -(size or 0), name)


def row_order(row: Detail) -> tuple[bool, int, str]:
    """Sort key putting the largest row first, unknown sizes last, and ties by label."""
    return largest_first(size=row.size_bytes, name=row.label)


def default_details(sizes: PathSizes) -> list[Detail]:
    """Describe an action without its own hook as one row per path.

    A row is labeled with the item's name and shows its folder as the item,
    because a full path under a deep shared folder is too long for one line.
    Rows merge on the full path, so same-named items in different folders
    stay apart.

    Args:
        sizes: Each resolved path's measured size.

    Returns:
        list[Detail]: One row per path, largest first.
    """
    rows = [
        Detail(
            label=path.name or tilde(path),
            size_bytes=size,
            items=(path.parent.name,) if path.parent.name else (),
            paths=(path,),
            key=str(path),
        )
        for path, size in sizes.items()
    ]
    return sorted(rows, key=row_order)


def action_details(action: Action, sizes: PathSizes, settings: PluginSettings) -> list[Detail]:
    """Describe an action's size as rows, using its own hook when it has one.

    A hook is plugin-supplied code, so one that raises falls back to the
    default rows rather than failing the run.

    Args:
        action: The measured action.
        sizes: Each resolved path's measured size.
        settings: The plugin's settings for this run.

    Returns:
        list[Detail]: The rows, largest first.
    """
    if action.details is None:
        return default_details(sizes)
    try:
        rows = list(action.details(sizes, settings))
        if not all(isinstance(row, Detail) for row in rows):
            msg = f"expected Detail rows, got {rows!r}"
            raise TypeError(msg)  # noqa: TRY301 - one fallback for every bad result
        return sorted(rows, key=row_order)
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        logger.debug("cannot describe %s: %s", action.name, exc)
        return default_details(sizes)


def merge(rows: Iterable[Detail]) -> list[Detail]:
    """Combine rows that describe the same thing, such as one project's rows from two actions.

    Args:
        rows: Rows from any number of actions.

    Returns:
        list[Detail]: One row per key, largest first.
    """
    merged: dict[str, Detail] = {}
    for row in rows:
        key = row.key or row.label
        first = merged.get(key)
        if first is None:
            merged[key] = row
            continue
        merged[key] = Detail(
            label=first.label,
            size_bytes=sum_known([first.size_bytes, row.size_bytes]),
            items=tuple(dict.fromkeys((*first.items, *row.items))),
            paths=(*first.paths, *row.paths),
            key=first.key,
        )
    return sorted(merged.values(), key=row_order)
