"""Discover plugins and decide which apply to this machine."""

import importlib
import logging
import pkgutil
import sys
import threading
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import cache
from types import ModuleType

import psutil
from nclutils.sh import which

from purger import plugins as _builtin_plugins
from purger.concurrency import map_as_completed
from purger.plugin import Plugin, PluginSettings
from purger.reports import SkippedPlugin, SkipReason

logger = logging.getLogger(__name__)


class DuplicatePluginError(ValueError):
    """Raised when two plugin modules declare the same plugin name."""


class UnknownPluginError(ValueError):
    """Raised when a requested plugin name is not in the registry."""


def discover_plugins(package: ModuleType | None = None) -> dict[str, Plugin]:
    """Import every module in a package and collect its module-level PLUGIN.

    Discovery by import keeps plugins declarative: adding a file is the only
    step needed to register one. Modules whose names start with an underscore,
    such as the plugin template, are never imported.

    Args:
        package: Package to scan. Defaults to `purger.plugins`.

    Returns:
        Plugins keyed by name, ordered by name.

    Raises:
        DuplicatePluginError: If two modules declare the same plugin name.
        TypeError: If a module's PLUGIN is not a Plugin.
    """
    if package is None:
        return dict(_builtin_registry())
    return _discover(package)


@cache
def _builtin_registry() -> dict[str, Plugin]:
    """Discover the built-in plugins once, since their modules never change in a run."""
    return _discover(_builtin_plugins)


def _discover(package: ModuleType) -> dict[str, Plugin]:
    found: dict[str, Plugin] = {}
    defined_in: dict[str, str] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue
        if not isinstance(plugin, Plugin):
            msg = f"{module.__name__}.PLUGIN must be a Plugin, got {type(plugin).__name__}"
            raise TypeError(msg)
        if plugin.name in found:
            msg = (
                f"duplicate plugin name {plugin.name!r} in {defined_in[plugin.name]} "
                f"and {module.__name__}"
            )
            raise DuplicatePluginError(msg)
        found[plugin.name] = plugin
        defined_in[plugin.name] = module.__name__
    return dict(sorted(found.items()))


class ProcessTable:
    """Every process's argv, read once and shared by the busy checks of one scan.

    The table reads on first use, so a scan where no plugin needs it never
    lists processes. Make a new table when a fresh view is needed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[tuple[psutil.Process, list[str]]] | None = None

    def running(self, matches: Callable[[list[str]], bool]) -> bool:
        """Report whether a process outside purger's own subprocesses satisfies a check.

        Args:
            matches: Receives a process argv and returns True for the tool.

        Returns:
            bool: True when some readable process argv matches. Processes whose
                argv cannot be read are skipped.
        """
        own_pid = psutil.Process().pid
        return any(
            matches(cmdline) and not _spawned_by(proc, pid=own_pid)
            for proc, cmdline in self._snapshot()
        )

    def _snapshot(self) -> list[tuple[psutil.Process, list[str]]]:
        with self._lock:
            if self._rows is None:
                self._rows = [
                    (proc, cmdline)
                    for proc in psutil.process_iter(["cmdline"])
                    if (cmdline := proc.info["cmdline"])
                ]
            return self._rows


def is_busy(plugin: Plugin, *, processes: ProcessTable | None = None) -> bool:
    """Report whether the plugin's tool is running, so a cleanup now could remove files it uses.

    Args:
        plugin: Plugin to check.
        processes: A table shared across plugins. Defaults to a fresh one.

    Returns:
        bool: True when a matching process runs outside purger, or when the
            check fails, so no cleanup runs blind.
    """
    if plugin.skip_while_running is None:
        return False
    try:
        return (processes or ProcessTable()).running(plugin.skip_while_running)
    except Exception as exc:  # noqa: BLE001
        # Plugin-supplied code and the process scan must not abort the whole run.
        logger.debug("busy check for %s failed: %s", plugin.name, exc)
        return True


def skip_reason(
    plugin: Plugin, *, check_available: bool = True, processes: ProcessTable | None = None
) -> SkipReason | None:
    """Return why a plugin does not apply to this machine, or None if it does.

    Checks run cheapest first: platform, required binary, the plugin's
    `skip_unless` check, then a scan for a running copy of its tool.

    Args:
        plugin: Plugin to check.
        check_available: Run the `skip_unless` check and the process scan.
            Disable to avoid their cost.
        processes: A process table shared across plugins. Defaults to a fresh one.
    """
    if sys.platform not in {system.value for system in plugin.operating_systems}:
        return SkipReason.PLATFORM
    if plugin.requires_on_path is not None and which(plugin.requires_on_path) is None:
        return SkipReason.MISSING_BINARY
    if not check_available:
        return None
    if not _available(plugin) or is_busy(plugin, processes=processes):
        return SkipReason.UNAVAILABLE
    return None


def check_plugins(
    plugins: Sequence[Plugin], *, settings_for: Callable[[Plugin], PluginSettings]
) -> tuple[list[Plugin], list[SkippedPlugin]]:
    """Split plugins into those that can run here and those that cannot, keeping plugin order.

    A plugin can run when it applies to this machine and its configuration check
    passes. Some availability checks run the tool itself, so the checks run side
    by side and share one process table.

    Args:
        plugins: Plugins to check.
        settings_for: Resolves the settings each plugin's configuration check receives.

    Returns:
        tuple[list[Plugin], list[SkippedPlugin]]: The plugins that can run, and the
            plugins left out with the reason.
    """
    processes = ProcessTable()

    def reason_for(plugin: Plugin) -> SkipReason | None:
        reason = skip_reason(plugin, processes=processes)
        if reason is None and not is_configured(plugin, settings_for(plugin)):
            return SkipReason.NOT_CONFIGURED
        return reason

    reasons = dict(map_as_completed(reason_for, items=plugins))
    usable: list[Plugin] = []
    skipped: list[SkippedPlugin] = []
    for index, plugin in enumerate(plugins):
        if (reason := reasons[index]) is None:
            usable.append(plugin)
        else:
            skipped.append(SkippedPlugin(name=plugin.name, reason=reason))
    return usable, skipped


def _available(plugin: Plugin) -> bool:
    """Report whether the plugin's own `skip_unless` check passes."""
    if plugin.skip_unless is None:
        return True
    try:
        return plugin.skip_unless()
    except Exception as exc:  # noqa: BLE001
        # Plugin-supplied code must not abort the whole run.
        logger.debug("skip_unless for %s failed: %s", plugin.name, exc)
        return False


def _is_blank(value: object) -> bool:
    """Report whether a setting value is empty, counting whitespace-only text as empty."""
    return value == () or (isinstance(value, str) and not value.strip())


def is_configured(plugin: Plugin, settings: PluginSettings) -> bool:
    """Report whether the plugin has the settings it needs to run.

    Args:
        plugin: Plugin to check.
        settings: The plugin's resolved settings for this run.

    Returns:
        bool: False when a required setting is empty or blank, or the plugin's
            `skip_unless_configured` hook returns False or raises.
    """
    if any(item.required and _is_blank(settings.value(item.name)) for item in plugin.settings):
        return False
    if plugin.skip_unless_configured is None:
        return True
    try:
        return plugin.skip_unless_configured(settings)
    except Exception as exc:  # noqa: BLE001
        # Plugin-supplied code must not abort the whole run.
        logger.warning("configuration check for %s failed: %s", plugin.name, exc)
        return False


def _spawned_by(proc: psutil.Process, *, pid: int) -> bool:
    """Report whether a process is pid itself or one of its descendants.

    Purger runs tool commands such as `uv cache size` while it measures, and
    those must not make the tool look busy.
    """
    try:
        return proc.pid == pid or any(parent.pid == pid for parent in proc.parents())
    except psutil.NoSuchProcess:
        # An ancestor can exit mid-walk too. Only the process itself exiting
        # means it can no longer be using the tool's files.
        return not proc.is_running()


@dataclass(frozen=True)
class Selection:
    """The plugins a run measures, and the plugins the skip setting turned off."""

    plugins: list[Plugin]
    disabled: list[SkippedPlugin]


def select_plugins(
    registry: dict[str, Plugin],
    names: Sequence[str],
    *,
    skip: AbstractSet[str] = frozenset(),
) -> Selection:
    """Narrow a registry to the requested plugin names and drop the disabled ones.

    The skip setting applies only to a run that names no plugins, so naming a
    plugin runs it regardless. Disabled plugins never reach `skip_reason`, so
    none of their hooks run.

    Args:
        registry: All known plugins keyed by name.
        names: Requested names. Empty selects every plugin.
        skip: Plugin names the skip setting turns off.

    Returns:
        Selection: Plugins in the order requested, without repeats, and the
            disabled plugins in registry order.

    Raises:
        UnknownPluginError: If any name is not in the registry.
    """
    if not names:
        return Selection(
            plugins=[plugin for name, plugin in registry.items() if name not in skip],
            disabled=[
                SkippedPlugin(name=name, reason=SkipReason.DISABLED)
                for name in registry
                if name in skip
            ],
        )
    requested = list(dict.fromkeys(names))
    unknown = [name for name in requested if name not in registry]
    if unknown:
        msg = f"unknown plugin(s): {', '.join(unknown)}. valid plugins: {', '.join(registry)}"
        raise UnknownPluginError(msg)
    return Selection(plugins=[registry[name] for name in requested], disabled=[])
