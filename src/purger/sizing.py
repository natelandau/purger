"""Measure how much space actions and plugins occupy."""

import logging
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from os import PathLike
from pathlib import Path

from nclutils.sh import ShellCommandError, run_command

from purger.config import PluginSettings
from purger.plugin import Action, Plugin, Tier
from purger.reports import ActionReport, PluginReport, sum_known
from purger.safety import has_symlinked_parent, invoking_home

logger = logging.getLogger(__name__)

_SIZE_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
GLOB_CHARS = frozenset("*?[")
_BLOCK_BYTES = 512
SIZE_CMD_TIMEOUT_SECONDS = 120

# Size command outputs keyed by command, with None for a command that failed.
type _SizeOutputs = dict[tuple[str, ...], str | None]
# The literal paths of each prepared action, keyed by action name.
type PreparedPaths = Mapping[str, tuple[Path, ...]]
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


def printed_dir_usage(output: str, patterns: Sequence[str] = ()) -> int:
    """Measure the directory a tool prints, such as `npm config get cache`.

    Use it as a `parse_size` hook, so a plugin sizes the directory the tool
    actually uses rather than a guessed default location.

    Args:
        output: Tool output whose first non-blank line is an absolute directory.
        patterns: Globs relative to that directory. Empty measures the whole directory.

    Returns:
        int: Allocated bytes, or 0 when the directory does not exist.

    Raises:
        ValueError: If the output does not name an absolute path.
    """
    line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    directory = Path(line)
    if not directory.is_absolute():
        msg = f"expected an absolute directory, got {line!r}"
        raise ValueError(msg)
    # The tool cleans the real directory, so measure it even when the printed one is a symlink.
    directory = directory.resolve()
    if not patterns:
        return disk_usage([directory])
    return disk_usage(match for pattern in patterns for match in directory.glob(pattern))


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
        return run_command(list(cmd), timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout
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


def measure_action(
    action: Action,
    *,
    size_outputs: _SizeOutputs | None = None,
    prepared: Sequence[Path] = (),
) -> ActionReport:
    """Measure one action, preferring its size hook, then size_cmd, then its paths.

    Args:
        action: The action to measure.
        size_outputs: Outputs of size commands already run in this measurement,
            so a command that several sizes read runs only once.
        prepared: The literal paths the plugin's `prepare` hook returned for a
            `prepared` action. Ignored for other actions.

    Returns:
        ActionReport: The size in bytes (None when unknown) and the resolved paths.
    """
    if action.prepared:
        resolved = _deletable_matches(prepared)
        has_paths = True
    else:
        patterns = _path_patterns(action)
        resolved = _deletable_matches(expand_paths(patterns)) if patterns else []
        has_paths = patterns is not None and bool(action.paths)
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
    elif has_paths:
        size = disk_usage(resolved)
    return ActionReport(action=action, size_bytes=size, resolved_paths=resolved)


def _path_patterns(action: Action) -> tuple[str, ...] | None:
    """Return the action's path globs, calling its paths function when it has one.

    Returns:
        tuple[str, ...] | None: The globs, or None when the paths function fails.
    """
    if isinstance(action.paths, tuple):
        return action.paths
    try:
        return tuple(action.paths())
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        logger.debug("cannot list paths for %s: %s", action.name, exc)
        return None


def _included_actions(plugin: Plugin, *, include_dangerous: bool) -> list[Action]:
    return [a for a in plugin.actions if include_dangerous or a.tier is not Tier.DANGEROUS]


def _footprint(plugin: Plugin, reports: list[ActionReport], outputs: _SizeOutputs) -> int | None:
    """Combine the plugin's own size, or else its action sizes, into one footprint."""
    if not plugin.has_own_footprint:
        return sum_known(r.size_bytes for r in reports)
    if plugin.size is not None:
        own = _guarded(label=plugin.name, size_fn=plugin.size)
    elif plugin.size_cmd is not None:
        own = _run_size_cmd(
            label=plugin.name, cmd=plugin.size_cmd, parser=plugin.parse_size, outputs=outputs
        )
    else:
        own = disk_usage(expand_paths(plugin.paths))
    if own is None:
        return None
    outside = sum_known(r.size_bytes for r in reports if r.action.outside_footprint)
    return own + (outside or 0)


class NotConfiguredError(Exception):
    """Raised when a plugin's prepare hook reports that a required setting is missing."""


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


def _checked_prepared(plugin: Plugin, result: object) -> dict[str, tuple[Path, ...]]:
    """Validate a prepare hook's result and coerce each value to a tuple of paths.

    Paths for a name that is no prepared action of the plugin are logged and dropped,
    since no action would ever measure or delete them.

    Raises:
        TypeError: If the result is not a mapping of action names to iterables of paths.
    """
    if not isinstance(result, Mapping):
        msg = f"expected a mapping of action names to paths, got {type(result).__name__}"
        raise TypeError(msg)
    prepared_names = {action.name for action in plugin.actions if action.prepared}
    checked: dict[str, tuple[Path, ...]] = {}
    for name, value in result.items():
        if not isinstance(name, str):
            msg = f"expected an action name, got {name!r}"
            raise TypeError(msg)
        # A string is iterable, so it would otherwise split into one-character paths.
        if isinstance(value, str | bytes):
            msg = f"expected paths for {name!r}, got {value!r}"
            raise TypeError(msg)
        paths = tuple(_as_path(item) for item in value)
        if name not in prepared_names:
            logger.warning(
                "prepare for %s returned paths for %r, which is not a prepared action",
                plugin.name,
                name,
            )
            continue
        checked[name] = paths
    return checked


def _prepare(plugin: Plugin, settings: PluginSettings) -> PreparedPaths | None:
    """Run the plugin's prepare hook with its settings for this run.

    A hook that raises or returns a malformed result is logged and the plugin
    continues with no prepared paths, so plugin-supplied code cannot abort the
    whole run.

    Returns:
        PreparedPaths | None: The paths by action name, empty when the plugin
            has no hook or the hook failed, or None when the hook reports the
            plugin as not configured.
    """
    if plugin.prepare is None:
        return {}
    try:
        result = plugin.prepare(settings)
        return None if result is None else _checked_prepared(plugin=plugin, result=result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare for %s failed: %s", plugin.name, exc)
        return {}


def _measure_actions(
    actions: Iterable[Action], *, outputs: _SizeOutputs, prepared_paths: PreparedPaths
) -> list[ActionReport]:
    return [
        measure_action(action, size_outputs=outputs, prepared=prepared_paths.get(action.name, ()))
        for action in actions
    ]


def measure_plugin(
    plugin: Plugin, *, include_dangerous: bool, settings: PluginSettings
) -> PluginReport:
    """Measure a plugin and its actions.

    The footprint is the plugin's own size when it declares one, plus the known
    sizes of measured actions marked `outside_footprint`. Without its own size,
    the footprint is the sum of its known action sizes. A size command shared by
    the plugin and its actions runs once. The `prepare` hook runs first, and
    `prepared` actions measure the paths it returned.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.
        settings: The plugin's settings for this run, handed to `prepare`.

    Returns:
        PluginReport: The footprint, per-action reports, and the prepared paths.

    Raises:
        NotConfiguredError: If the `prepare` hook reports the plugin as not configured.
    """
    prepared_paths = _prepare(plugin=plugin, settings=settings)
    if prepared_paths is None:
        msg = f"plugin {plugin.name!r} is not configured"
        raise NotConfiguredError(msg)
    outputs: _SizeOutputs = {}
    reports = _measure_actions(
        _included_actions(plugin, include_dangerous=include_dangerous),
        outputs=outputs,
        prepared_paths=prepared_paths,
    )
    return PluginReport(
        plugin=plugin,
        footprint_bytes=_footprint(plugin=plugin, reports=reports, outputs=outputs),
        actions=reports,
        prepared_paths=prepared_paths,
    )


def measure_footprint(
    plugin: Plugin, *, include_dangerous: bool, prepared_paths: PreparedPaths
) -> int | None:
    """Measure only a plugin's footprint, as `measure_plugin` would report it.

    Use this to re-measure after a cleanup: a plugin that sizes itself skips
    every action except those marked `outside_footprint`. The `prepare` hook
    does not run again; `prepared` actions re-measure the given paths, so freed
    space compares the same paths before and after.

    Args:
        plugin: The plugin to measure.
        include_dangerous: Include DANGEROUS actions when true.
        prepared_paths: The prepared paths from the plugin's first measurement.

    Returns:
        int | None: The footprint in bytes, or None when unknown.
    """
    actions = _included_actions(plugin, include_dangerous=include_dangerous)
    if plugin.has_own_footprint:
        actions = [action for action in actions if action.outside_footprint]
    outputs: _SizeOutputs = {}
    reports = _measure_actions(actions, outputs=outputs, prepared_paths=prepared_paths)
    return _footprint(plugin=plugin, reports=reports, outputs=outputs)
