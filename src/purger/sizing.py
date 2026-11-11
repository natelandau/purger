"""Measure how much space actions and plugins occupy."""

import logging
import re
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import partial
from os import PathLike
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.details import action_details
from purger.plugin import (
    Action,
    DeletePaths,
    FoundBy,
    Plugin,
    PluginSettings,
    RiskLevel,
    SizeComputedBy,
    SizeOfDirectoryPrintedBy,
    SizeOfPaths,
    SizeReportedByCommand,
    SizeSource,
    SizeUnknown,
    _PathSelection,
    first_report_of,
)
from purger.reports import ActionReport, PluginReport, sum_known
from purger.safety import has_symlinked_parent, invoking_home

logger = logging.getLogger(__name__)

_SIZE_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
GLOB_CHARS = frozenset("*?[")
_BLOCK_BYTES = 512
SIZE_COMMAND_TIMEOUT_SECONDS = 120

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
        if not GLOB_CHARS.isdisjoint(part):
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


def any_path_exists(patterns: Sequence[str]) -> bool:
    """Report whether any pattern names an existing path, stopping at the first match.

    Use it for a cheap `skip_unless` check, since it neither lists nor sorts
    every match the way `expand_paths` does. Patterns expand as in `expand_paths`.

    Args:
        patterns: Literal paths or glob patterns.

    Returns:
        bool: True when at least one pattern matches a path, including a dangling symlink.
    """
    for pattern in patterns:
        try:
            base, glob = _split_pattern(pattern)
        except RuntimeError as exc:
            logger.debug("cannot expand %s: %s", pattern, exc)
            continue
        if glob is None:
            if base.exists() or base.is_symlink():
                return True
        elif next(base.glob(glob), None) is not None:
            return True
    return False


def disk_usage(paths: Iterable[Path]) -> int:
    """Sum the allocated disk space of paths without following symlinks.

    Counting blocks matches what deletion frees, and counting each inode once
    avoids double-counting hard links.

    Args:
        paths: Files or directories to measure.

    Returns:
        int: Allocated bytes.
    """
    return sum(disk_usage_by_path(paths).values())


def disk_usage_by_path(paths: Iterable[Path]) -> dict[Path, int]:
    """Measure each path's allocated disk space in one walk, so per-path sizes add up.

    Each inode counts once across all paths, toward the first path that reaches
    it, so the sizes sum to `disk_usage` over the same paths.

    Args:
        paths: Files or directories to measure.

    Returns:
        dict[Path, int]: Allocated bytes per input path, 0 for one that cannot be read.
    """
    seen: set[tuple[int, int]] = set()
    sizes: dict[Path, int] = {}
    for top in paths:
        total = 0
        stack: list[Path] = [top]
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
        sizes[top] = sizes.get(top, 0) + total
    return sizes


def nonblank_lines(output: str) -> list[str]:
    """Split command output into its lines, stripped, leaving out blank ones.

    Args:
        output: Text a command printed.

    Returns:
        list[str]: Each non-blank line without surrounding whitespace.
    """
    return [stripped for line in output.splitlines() if (stripped := line.strip())]


def printed_directory(output: str, *, single_line: bool = False) -> Path:
    """Read the directory a tool prints, such as `npm config get cache`, at its real path.

    Args:
        output: Tool output whose first non-blank line is an absolute directory.
        single_line: Refuse output with more than one non-blank line, for a
            caller that acts on the answer and must not guess.

    Returns:
        Path: The directory with symlinks resolved, since the tool works on the real one.

    Raises:
        ValueError: If the output does not name an absolute path, or holds
            several lines when `single_line` is set.
    """
    lines = nonblank_lines(output)
    if single_line and len(lines) != 1:
        msg = f"expected one absolute directory, got {output!r}"
        raise ValueError(msg)
    directory = Path(lines[0]) if lines else Path()
    if not directory.is_absolute():
        msg = f"expected an absolute directory, got {output!r}"
        raise ValueError(msg)
    return directory.resolve()


def _printed_directory_usage(output: str, *, subpaths: Sequence[str]) -> int:
    """Measure the directory a tool prints, such as `npm config get cache`.

    Args:
        output: Tool output whose first non-blank line is an absolute directory.
        subpaths: Globs relative to that directory. Empty measures the whole directory.

    Returns:
        int: Allocated bytes, or 0 when the directory does not exist.

    Raises:
        ValueError: If the output does not name an absolute directory.
    """
    directory = printed_directory(output)
    if not subpaths:
        return disk_usage([directory])
    return disk_usage(match for pattern in subpaths for match in directory.glob(pattern))


def _guarded(label: str, size_fn: Callable[[], int | None]) -> int | None:
    """Run plugin-supplied sizing code, treating a failure or a negative size as unknown."""
    try:
        size = size_fn()
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        logger.debug("cannot size %s: %s", label, exc)
        return None
    if size is not None and size < 0:
        logger.debug("negative size %d for %s treated as unknown", size, label)
        return None
    return size


def _size_command_output(label: str, command: tuple[str, ...]) -> str | None:
    """Run a size command and return its output, or None when it fails."""
    try:
        return run_command(list(command), timeout=SIZE_COMMAND_TIMEOUT_SECONDS).stdout
    except ShellCommandError as exc:
        logger.debug("size command for %s failed: %s", label, exc)
    except UnicodeDecodeError as exc:
        logger.debug("size command for %s printed output that is not UTF-8: %s", label, exc)
    return None


def _command_output(label: str, command: tuple[str, ...], *, outputs: _SizeOutputs) -> str | None:
    """Return a size command's output, running the command only if outputs lacks it."""
    if command not in outputs:
        outputs[command] = _size_command_output(label=label, command=command)
    return outputs[command]


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


def _as_path(item: object) -> Path:
    """Convert one returned path, rejecting anything that is not an absolute string or path.

    Raises:
        TypeError: If the item is neither a string nor a path.
        ValueError: If the path is not absolute.
    """
    if not isinstance(item, str | PathLike):
        msg = f"expected a path, got {item!r}"
        raise TypeError(msg)
    path = Path(item)
    # A relative path would be measured against the working directory, which deletion refuses.
    if not path.is_absolute():
        msg = f"expected an absolute path, got {item!r}"
        raise ValueError(msg)
    return path


def _found_paths(
    found_by: FoundBy, *, label: str, settings: PluginSettings, deleted: bool
) -> list[Path] | None:
    """Run a plugin's `found_by` and check that it returned absolute paths.

    A failure is a warning when purger deletes the paths, and a debug line when
    the tool's own command deletes them and purger only sizes them.

    Returns:
        list[Path] | None: The paths once each, or None when the function raised
            or returned anything but absolute paths, so the action offers nothing.
    """
    try:
        found = found_by(settings)
        # A string is iterable, so it would otherwise split into one-character paths.
        if isinstance(found, str | bytes):
            msg = f"expected paths, got {found!r}"
            raise TypeError(msg)  # noqa: TRY301 - one log line for every bad result
        # A path returned twice would be counted and listed twice.
        return list(dict.fromkeys(_as_path(item) for item in found))
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        if not deleted:
            logger.debug("cannot find paths for %s: %s", label, exc)
        elif first_report_of(exc):
            logger.warning("cannot find paths for %s: %s", label, exc)
        return None


def _matches(
    selection: _PathSelection, *, label: str, settings: PluginSettings
) -> list[Path] | None:
    """Return what a path selection names on disk, or None when its `found_by` failed."""
    if selection.found_by is not None:
        return _found_paths(
            selection.found_by,
            label=label,
            settings=settings,
            deleted=isinstance(selection, DeletePaths),
        )
    return expand_paths(selection.globs)


@dataclass(frozen=True)
class _Measurement:
    """A measured size, with the paths it covers and each path's share when it came from paths."""

    size_bytes: int | None
    paths: list[Path] = field(default_factory=list)
    path_sizes: dict[Path, int] = field(default_factory=dict)


def _measure_paths(paths: list[Path]) -> _Measurement:
    """Measure paths in one walk that also gives each path's share for detail rows."""
    path_sizes = disk_usage_by_path(paths)
    return _Measurement(size_bytes=sum(path_sizes.values()), paths=paths, path_sizes=path_sizes)


def _measure_size(
    source: SizeSource | None,
    *,
    label: str,
    settings: PluginSettings,
    outputs: _SizeOutputs,
) -> _Measurement:
    """Measure one size object, with an unknown size when it is unknown or cannot be read."""
    size: int | None = None
    match source:
        case SizeOfPaths():
            matches = _matches(source, label=label, settings=settings)
            return _Measurement(size_bytes=None) if matches is None else _measure_paths(matches)
        case SizeComputedBy(function=function):
            size = _guarded(label=label, size_fn=function)
        case SizeReportedByCommand(command=command, read_size=read_size):
            stdout = _command_output(label=label, command=command, outputs=outputs)
            if stdout is not None:
                size = _guarded(label=label, size_fn=partial(read_size, stdout))
        case SizeOfDirectoryPrintedBy(command=command, subpaths=subpaths):
            stdout = _command_output(label=label, command=command, outputs=outputs)
            if stdout is not None:
                read = partial(_printed_directory_usage, stdout, subpaths=subpaths)
                size = _guarded(label=label, size_fn=read)
        case None | SizeUnknown():
            pass
    return _Measurement(size_bytes=size)


def measure_action(
    action: Action,
    *,
    settings: PluginSettings,
    size_outputs: _SizeOutputs | None = None,
    with_details: bool = False,
) -> ActionReport:
    """Measure one action and resolve the paths it deletes or is sized by.

    Args:
        action: The action to measure.
        settings: The plugin's settings for this run, which `found_by` functions receive.
        size_outputs: Outputs of size commands already run in this measurement,
            so a command that several sizes read runs only once.
        with_details: Also build the rows that break the size down.

    Returns:
        ActionReport: The size in bytes (None when unknown), the resolved paths,
            and the detail rows when asked for.
    """
    if isinstance(action.cleanup, DeletePaths):
        matches = _matches(action.cleanup, label=action.name, settings=settings)
        # Deleting nothing frees nothing, so a failed `found_by` offers nothing.
        measured = (
            _Measurement(size_bytes=0)
            if matches is None
            else _measure_paths(_deletable_matches(matches))
        )
    else:
        measured = _measure_size(
            action.size,
            label=action.name,
            settings=settings,
            outputs={} if size_outputs is None else size_outputs,
        )
    report = ActionReport(
        action=action, size_bytes=measured.size_bytes, resolved_paths=measured.paths
    )
    if with_details and (measured.path_sizes or action.size_breakdown is not None):
        report.details = action_details(action=action, sizes=measured.path_sizes, settings=settings)
    return report


def _actions_to_measure(
    plugin: Plugin, *, include_dangerous: bool, total_only: bool
) -> list[Action]:
    """Pick the actions a measurement of the plugin includes.

    DANGEROUS actions count only when asked for. When only the plugin's size
    is wanted and the plugin sets `total_size`, the only actions that matter
    are those marked `adds_to_plugin_total_size`.

    Args:
        plugin: The plugin being measured.
        include_dangerous: Include DANGEROUS actions when true.
        total_only: Measure only what the plugin's size needs.
    """
    return [
        action
        for action in plugin.actions
        if (include_dangerous or action.risk_level is not RiskLevel.DANGEROUS)
        and (not total_only or plugin.total_size is None or action.adds_to_plugin_total_size)
    ]


def _plugin_total(
    plugin: Plugin, *, reports: list[ActionReport], settings: PluginSettings, outputs: _SizeOutputs
) -> int | None:
    """Combine the plugin's total size, or else its action sizes, into one figure."""
    if plugin.total_size is None:
        return sum_known(r.size_bytes for r in reports)
    own = _measure_size(
        plugin.total_size, label=plugin.name, settings=settings, outputs=outputs
    ).size_bytes
    if own is None:
        return None
    outside = sum_known(r.size_bytes for r in reports if r.action.adds_to_plugin_total_size)
    return own + (outside or 0)


def measure_plugin(
    plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings, with_details: bool = False
) -> PluginReport:
    """Measure a plugin and its actions.

    The plugin's size is its `total_size` when it declares one, plus the known
    sizes of measured actions marked `adds_to_plugin_total_size`. Without a
    `total_size`, it is the sum of the known action sizes. A size command shared
    by the plugin and its actions runs once.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.
        settings: The plugin's settings for this run.
        with_details: Also build each action's detail rows.

    Returns:
        PluginReport: The plugin's size, per-action reports, and the settings measured with.
    """
    outputs: _SizeOutputs = {}
    reports = [
        measure_action(action, settings=settings, size_outputs=outputs, with_details=with_details)
        for action in _actions_to_measure(
            plugin, include_dangerous=include_dangerous, total_only=False
        )
    ]
    return PluginReport(
        plugin=plugin,
        total_size_bytes=_plugin_total(plugin, reports=reports, settings=settings, outputs=outputs),
        actions=reports,
        settings=settings,
    )


def measure_total_size(
    plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings
) -> int | None:
    """Measure only a plugin's size, as `measure_plugin` would report it.

    Use this to re-measure after a cleanup: a plugin with a `total_size` skips
    every action except those marked `adds_to_plugin_total_size`. A scan that
    runs `once_per_run` is not repeated, so freed space compares the same paths
    before and after.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.
        settings: The plugin's settings from its first measurement.

    Returns:
        int | None: The size in bytes, or None when unknown.
    """
    actions = _actions_to_measure(plugin, include_dangerous=include_dangerous, total_only=True)
    outputs: _SizeOutputs = {}
    reports = [
        measure_action(action, settings=settings, size_outputs=outputs) for action in actions
    ]
    return _plugin_total(plugin, reports=reports, settings=settings, outputs=outputs)
