"""Plugin for old browser builds that Playwright leaves in its shared cache."""

import re
import sys
from collections import defaultdict
from pathlib import Path

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    PluginSettings,
    RiskLevel,
    folder_from_environment,
)
from purger.safety import invoking_home

_BUILD = re.compile(r"^(?P<browser>.+)-(?P<revision>\d+)$")


def _registry_dir(platform: str) -> str:
    """Return the shared browser cache that Playwright installs into on a platform.

    Args:
        platform: A `sys.platform` value.

    Returns:
        str: The cache folder, from `PLAYWRIGHT_BROWSERS_PATH` when it is absolute.
    """
    default = (
        "~/Library/Caches/ms-playwright"
        if platform == "darwin"
        else f"{folder_from_environment('XDG_CACHE_HOME', default='~/.cache')}/ms-playwright"
    )
    return folder_from_environment("PLAYWRIGHT_BROWSERS_PATH", default=default)


def _old_builds(builds: list[Path]) -> list[Path]:
    """Pick every browser build older than the newest build of the same browser.

    Each Playwright release pins one revision of each browser, such as
    `chromium-1254`, and an install only removes the builds no live install
    links to, so builds for projects that no longer upgrade pile up.

    Args:
        builds: Entries of the browser cache.

    Returns:
        list[Path]: The older builds, sorted.
    """
    by_browser: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for build in builds:
        if match := _BUILD.match(build.name):
            by_browser[match["browser"]].append((int(match["revision"]), build))
    old: list[Path] = []
    for revisions in by_browser.values():
        old.extend(path for _, path in sorted(revisions)[:-1])
    return sorted(old)


def list_old_browser_builds() -> list[Path]:
    """List the old browser builds now in the cache.

    Returns:
        list[Path]: Each build older than the newest of its browser.
    """
    registry = Path(_registry_dir(sys.platform))
    try:
        if registry.parts[0] == "~":
            registry = invoking_home().joinpath(*registry.parts[1:])
        else:
            # Resolves `~user` forms, which `invoking_home` does not cover.
            registry = registry.expanduser()
        entries = list(registry.iterdir())
    except (OSError, RuntimeError):
        # No cache folder, or no home folder to find it in, means no builds.
        return []
    return _old_builds(entries)


def old_browser_builds_exist() -> bool:
    """Report whether the cache holds any old browser build."""
    return bool(list_old_browser_builds())


def find_old_browser_builds(settings: PluginSettings) -> list[Path]:  # noqa: ARG001
    """Find the old browser builds now in the cache.

    Args:
        settings: The plugin's settings, which this plugin does not use.

    Returns:
        list[Path]: Each build older than the newest of its browser.
    """
    return list_old_browser_builds()


PLUGIN = Plugin(
    name="playwright",
    description="Playwright browser builds",
    skip_unless=old_browser_builds_exist,
    actions=(
        Action(
            name="old-browsers",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove each browser build older than the newest one. A project pinned "
                "to an older Playwright needs `playwright install` before its next run."
            ),
            cleanup=DeletePaths(found_by=find_old_browser_builds),
        ),
    ),
)
