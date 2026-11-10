"""Plugin for the Yarn global package caches."""

from purger.plugin import Action, Plugin, Tier
from purger.sizing import expand_paths

# Yarn 1 cache on macOS and Linux, then the Yarn 2+ global mirror. Deleted by path
# so the plugin works without running yarn, which a corepack shim may try to download.
_CACHE_PATHS = ("~/Library/Caches/Yarn/*", "~/.cache/yarn/*", "~/.yarn/berry/cache/*")


def _has_cache() -> bool:
    """Report whether a Yarn cache exists, since the plugin does not need yarn installed.

    Returns:
        bool: True when any cache path has contents.
    """
    return bool(expand_paths(_CACHE_PATHS))


PLUGIN = Plugin(
    name="yarn",
    description="Yarn global package cache",
    is_available=_has_cache,
    actions=(
        Action(
            name="cache",
            tier=Tier.CAUTION,
            help="Wipe the global Yarn cache. Packages download again on next install.",
            paths=_CACHE_PATHS,
        ),
    ),
)
