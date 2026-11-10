"""Plugin for the pip download and wheel cache."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.sizing import expand_paths


def _cache_patterns() -> tuple[str, ...]:
    """List the pip cache contents to delete, as glob-safe patterns.

    Deleted by path so a cache left behind after pip itself is gone still gets
    cleaned. On Linux pip follows `XDG_CACHE_HOME`, and `~/.cache/pip` is kept
    for a cache written before the variable was set.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    xdg_cache = env_dir("XDG_CACHE_HOME", "~/.cache")
    return tuple(dict.fromkeys(("~/Library/Caches/pip/*", "~/.cache/pip/*", f"{xdg_cache}/pip/*")))


_CACHE_PATHS = _cache_patterns()


def _has_cache() -> bool:
    """Report whether a pip cache exists, since the plugin does not need pip installed.

    Returns:
        bool: True when any cache path has contents.
    """
    return bool(expand_paths(_CACHE_PATHS))


PLUGIN = Plugin(
    name="pip",
    description="pip download and wheel cache",
    is_available=_has_cache,
    actions=(
        Action(
            name="cache",
            tier=Tier.CAUTION,
            help="Wipe the pip cache. Packages download and build again on next install.",
            paths=_CACHE_PATHS,
        ),
    ),
)
