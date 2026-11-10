"""Measure how much space actions and plugins occupy."""

import logging
import re
import stat
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import Action, Plugin, Tier
from purger.reports import ActionReport, PluginReport, sum_known
from purger.safety import has_symlinked_parent, invoking_home

logger = logging.getLogger(__name__)

_SIZE_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
_GLOB_CHARS = frozenset("*?[")
_BLOCK_BYTES = 512
_SIZE_CMD_TIMEOUT_SECONDS = 120

# Size command outputs keyed by command, with None for a command that failed.
type _SizeOutputs = dict[tuple[str, ...], str | None]
_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_human_size(text: str) -> int:
    """Convert a human-readable size such as "1.2 GiB" into bytes.

    Tools report sizes as text; this gives size commands one shared parser.

    Args:
        text: The size string, with an optional unit.

    Returns:
        int: The size in bytes.

    Raises:
        ValueError: If the text is not a number with a known unit.
    """
    match = _SIZE_PATTERN.match(text)
    if match is None:
        msg = f"cannot parse size {text!r}: expected a number with an optional unit"
        raise ValueError(msg)
    factor = _UNITS.get(match.group(2).lower())
    if factor is None:
        msg = f"unknown size unit {match.group(2)!r} in {text!r}"
        raise ValueError(msg)
    return int(float(match.group(1)) * factor)


def _split_pattern(pattern: str) -> tuple[Path, str | None]:
    """Split a pattern into its literal base path and the glob that follows it, if any.

    Only the part after `~` is scanned for glob characters, so a home directory
    whose name holds `[`, `*`, or `?` is still taken literally.

    Raises:
        RuntimeError: If `~` cannot be expanded because no home directory is known.
    """
    path = Path(pattern)
    parts = path.parts
    if parts and parts[0] == "~":
        base, rest = invoking_home(), parts[1:]
    elif parts and parts[0].startswith("~"):
        base, rest = Path(parts[0]).expanduser(), parts[1:]
    elif path.anchor:
        base, rest = Path(parts[0]), parts[1:]
    else:
        base, rest = Path(), parts
    for index, part in enumerate(rest):
        if not _GLOB_CHARS.isdisjoint(part):
            return base.joinpath(*rest[:index]), "/".join(rest[index:])
    return base.joinpath(*rest), None


def expand_paths(patterns: Sequence[str]) -> list[Path]:
    """Expand `~` and globs into the existing paths they name.

    Dotfiles match globs, and dangling symlinks are kept so they can be cleaned.
    A pattern whose `~` cannot be expanded matches nothing.

    Args:
        patterns: Literal paths or glob patterns.

    Returns:
        list[Path]: Sorted, deduplicated, unresolved paths.
    """
    found: set[Path] = set()
    for pattern in patterns:
        try:
            base, glob = _split_pattern(pattern)
        except RuntimeError as exc:
            logger.debug("cannot expand %s: %s", pattern, exc)
            continue
        if glob is None:
            if base.exists() or base.is_symlink():
                found.add(base)
        else:
            found.update(base.glob(glob))
    return sorted(found)


def disk_usage(paths: Iterable[Path]) -> int:
    """Sum the allocated disk space of paths without following symlinks.

    Counting blocks matches what deletion frees, and counting each inode once
    avoids double-counting hard links.

    Args:
        paths: Files or directories to measure.

    Returns:
        int: Allocated bytes.
    """
    seen: set[tuple[int, int]] = set()
    total = 0
    stack: list[Path] = list(paths)
    while stack:
        path = stack.pop()
        try:
            info = path.lstat()
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                total += info.st_blocks * _BLOCK_BYTES
            if stat.S_ISDIR(info.st_mode):
                stack.extend(path.iterdir())
        except OSError as exc:
            logger.debug("cannot measure %s: %s", path, exc)
    return total


def _parse_int(text: str) -> int:
    return int(text.strip())


def _non_negative(label: str, size: int) -> int | None:
    """Treat a negative size as unknown."""
    if size < 0:
        logger.debug("negative size %d for %s treated as unknown", size, label)
        return None
    return size


def _guarded(label: str, size_fn: Callable[[], int | None]) -> int | None:
    """Run plugin-supplied sizing code, treating a failure or a negative size as unknown."""
    try:
        size = size_fn()
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        logger.debug("cannot size %s: %s", label, exc)
        return None
    return None if size is None else _non_negative(label=label, size=size)


def _size_cmd_output(label: str, cmd: tuple[str, ...]) -> str | None:
    """Run a size command and return its output, or None when it fails."""
    try:
        return run_command(list(cmd), timeout=_SIZE_CMD_TIMEOUT_SECONDS).stdout
    except ShellCommandError as exc:
        logger.debug("size command for %s failed: %s", label, exc)
    except UnicodeDecodeError as exc:
        logger.debug("size command for %s printed output that is not UTF-8: %s", label, exc)
    return None


def _run_size_cmd(
    label: str,
    cmd: tuple[str, ...],
    parser: Callable[[str], int] | None,
    *,
    outputs: _SizeOutputs,
) -> int | None:
    """Parse a size command's output, running the command only if outputs lacks it."""
    if cmd not in outputs:
        outputs[cmd] = _size_cmd_output(label=label, cmd=cmd)
    stdout = outputs[cmd]
    if stdout is None:
        return None
    return _guarded(label=label, size_fn=lambda: (parser or _parse_int)(stdout))


def _deletable_matches(paths: Iterable[Path]) -> list[Path]:
    """Drop matches that deletion refuses because a parent below home is a symlink."""
    try:
        home = invoking_home()
    except RuntimeError as exc:
        # Without a home no match lies below it, so none can pass through a symlink there.
        logger.debug("cannot check matches for symlinked parents: %s", exc)
        return list(paths)
    home_resolved = home.resolve()
    kept: list[Path] = []
    for path in paths:
        if has_symlinked_parent(path, home=home, home_resolved=home_resolved):
            logger.debug("ignoring %s: it is reached through a symlinked directory", path)
        else:
            kept.append(path)
    return kept


def measure_action(action: Action, *, size_outputs: _SizeOutputs | None = None) -> ActionReport:
    """Measure one action, preferring its size hook, then size_cmd, then its paths.

    Args:
        action: The action to measure.
        size_outputs: Outputs of size commands already run in this measurement,
            so a command that several sizes read runs only once.

    Returns:
        ActionReport: The size in bytes (None when unknown) and the resolved paths.
    """
    resolved = _deletable_matches(expand_paths(action.paths)) if action.paths else []
    size: int | None = None
    if action.size is not None:
        size = _guarded(label=action.name, size_fn=action.size)
    elif action.size_cmd is not None:
        size = _run_size_cmd(
            label=action.name,
            cmd=action.size_cmd,
            parser=action.parse_size,
            outputs={} if size_outputs is None else size_outputs,
        )
    elif action.paths:
        size = disk_usage(resolved)
    return ActionReport(action=action, size_bytes=size, resolved_paths=resolved)


def _included_actions(plugin: Plugin, *, include_dangerous: bool) -> list[Action]:
    return [a for a in plugin.actions if include_dangerous or a.tier is not Tier.DANGEROUS]


def _footprint(plugin: Plugin, reports: list[ActionReport], outputs: _SizeOutputs) -> int | None:
    """Combine the plugin's own size, or else its action sizes, into one footprint."""
    if not plugin.has_own_footprint:
        return sum_known(r.size_bytes for r in reports)
    if plugin.size_cmd is not None:
        own = _run_size_cmd(
            label=plugin.name, cmd=plugin.size_cmd, parser=plugin.parse_size, outputs=outputs
        )
    else:
        own = disk_usage(expand_paths(plugin.paths))
    if own is None:
        return None
    outside = sum_known(r.size_bytes for r in reports if r.action.outside_footprint)
    return own + (outside or 0)


def measure_plugin(plugin: Plugin, *, include_dangerous: bool) -> PluginReport:
    """Measure a plugin and its actions.

    The footprint is the plugin's own size when it declares one, plus the known
    sizes of measured actions marked `outside_footprint`. Without its own size,
    the footprint is the sum of its known action sizes. A size command shared by
    the plugin and its actions runs once.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.

    Returns:
        PluginReport: The footprint and per-action reports.
    """
    outputs: _SizeOutputs = {}
    reports = [
        measure_action(action, size_outputs=outputs)
        for action in _included_actions(plugin, include_dangerous=include_dangerous)
    ]
    return PluginReport(
        plugin=plugin,
        footprint_bytes=_footprint(plugin=plugin, reports=reports, outputs=outputs),
        actions=reports,
    )


def measure_footprint(plugin: Plugin, *, include_dangerous: bool) -> int | None:
    """Measure only a plugin's footprint, as `measure_plugin` would report it.

    Use this to re-measure after a cleanup: a plugin that sizes itself skips
    every action except those marked `outside_footprint`.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.

    Returns:
        int | None: The footprint in bytes, or None when unknown.
    """
    actions = _included_actions(plugin, include_dangerous=include_dangerous)
    if plugin.has_own_footprint:
        actions = [action for action in actions if action.outside_footprint]
    outputs: _SizeOutputs = {}
    reports = [measure_action(action, size_outputs=outputs) for action in actions]
    return _footprint(plugin=plugin, reports=reports, outputs=outputs)
