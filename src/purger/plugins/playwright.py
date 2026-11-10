"""Plugin for old browser builds that Playwright leaves in its shared cache."""

import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.sizing import expand_paths

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
        else f"{env_dir('XDG_CACHE_HOME', '~/.cache')}/ms-playwright"
    )
    return env_dir("PLAYWRIGHT_BROWSERS_PATH", default)


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


def _old_build_paths() -> tuple[str, ...]:
    """List the old browser builds now in the cache, as globs that match only themselves.

    Returns:
        tuple[str, ...]: One escaped path per old build.
    """
    registry = _registry_dir(sys.platform)
    builds = expand_paths((f"{registry}/*",))
    return tuple(glob.escape(str(path)) for path in _old_builds(builds))


PLUGIN = Plugin(
    name="playwright",
    description="Playwright browser builds",
    is_available=lambda: bool(_old_build_paths()),
    actions=(
        Action(
            name="old-browsers",
            tier=Tier.CAUTION,
            help=(
                "Remove each browser build older than the newest one. A project pinned "
                "to an older Playwright needs `playwright install` before its next run."
            ),
            paths=_old_build_paths,
        ),
    ),
)
