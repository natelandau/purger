"""Plugin for the Yarn global package caches."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.sizing import expand_paths


def _cache_patterns() -> tuple[str, ...]:
    """List the Yarn cache contents to delete, as glob-safe patterns.

    Covers the Yarn 1 cache on macOS and Linux, where it follows `XDG_CACHE_HOME`,
    then the Yarn 2+ global mirror. Deleted by path so the plugin works without
    running yarn, which a corepack shim may try to download.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    xdg_cache = env_dir("XDG_CACHE_HOME", "~/.cache")
    return tuple(
        dict.fromkeys(
            (
                "~/Library/Caches/Yarn/*",
                "~/.cache/yarn/*",
                f"{xdg_cache}/yarn/*",
                "~/.yarn/berry/cache/*",
            )
        )
    )


_CACHE_PATHS = _cache_patterns()


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
