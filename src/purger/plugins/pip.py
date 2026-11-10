"""Plugin for the pip download and wheel cache."""

from purger.plugin import Action, Plugin, Tier
from purger.sizing import expand_paths

# Deleted by path so a cache left behind after pip itself is gone still gets cleaned.
_CACHE_PATHS = ("~/Library/Caches/pip/*", "~/.cache/pip/*")


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
