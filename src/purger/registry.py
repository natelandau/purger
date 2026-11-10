"""Discover plugins and decide which apply to this machine."""

import importlib
import logging
import pkgutil
import sys
from collections.abc import Sequence
from functools import cache
from types import ModuleType

from nclutils.sh import which

from purger import plugins as _builtin_plugins
from purger.plugin import Plugin
from purger.reports import SkipReason

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


def skip_reason(plugin: Plugin, *, check_available: bool = True) -> SkipReason | None:
    """Return why a plugin does not apply to this machine, or None if it does.

    Checks run cheapest first: platform, required binary, then the plugin's
    own availability hook.

    Args:
        plugin: Plugin to check.
        check_available: Run the plugin's `is_available` hook. Disable to avoid its cost.
    """
    if sys.platform not in plugin.platforms:
        return SkipReason.PLATFORM
    if plugin.requires is not None and which(plugin.requires) is None:
        return SkipReason.MISSING_BINARY
    if check_available and plugin.is_available is not None:
        try:
            available = plugin.is_available()
        except Exception as exc:  # noqa: BLE001
            # Plugin-supplied code must not abort the whole run.
            logger.debug("is_available for %s failed: %s", plugin.name, exc)
            return SkipReason.UNAVAILABLE
        if not available:
            return SkipReason.UNAVAILABLE
    return None


def select_plugins(registry: dict[str, Plugin], names: Sequence[str]) -> list[Plugin]:
    """Narrow a registry to the requested plugin names.

    Args:
        registry: All known plugins keyed by name.
        names: Requested names. Empty selects every plugin.

    Returns:
        Plugins in the order requested, without repeats.

    Raises:
        UnknownPluginError: If any name is not in the registry.
    """
    if not names:
        return list(registry.values())
    requested = list(dict.fromkeys(names))
    unknown = [name for name in requested if name not in registry]
    if unknown:
        msg = f"unknown plugin(s): {', '.join(unknown)}. valid plugins: {', '.join(registry)}"
        raise UnknownPluginError(msg)
    return [registry[name] for name in requested]
