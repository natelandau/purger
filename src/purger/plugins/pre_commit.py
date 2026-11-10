"""Plugin for the pre-commit hook cache."""

import os
from pathlib import Path

from purger.plugin import Action, Plugin, Tier
from purger.safety import invoking_home
from purger.sizing import disk_usage


def _cache_dir() -> Path:
    """Return the directory pre-commit stores hook repos in, following its own lookup order."""
    if pre_commit_home := os.environ.get("PRE_COMMIT_HOME"):
        return Path(pre_commit_home)
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else invoking_home() / ".cache"
    return base / "pre-commit"


def _cache_size() -> int:
    """Measure the pre-commit cache.

    Returns:
        int: Allocated bytes, or 0 when the cache does not exist.
    """
    return disk_usage([_cache_dir().resolve()])


PLUGIN = Plugin(
    name="pre-commit",
    description="pre-commit hook environments and repos",
    requires="pre-commit",
    size=_cache_size,
    actions=(
        Action(
            name="gc",
            tier=Tier.SAFE,
            help="Remove hook repos no longer referenced by any config.",
            command=("pre-commit", "gc"),
        ),
        Action(
            name="clean",
            tier=Tier.DANGEROUS,
            help="Wipe the entire pre-commit cache. Hook environments are reinstalled on next run.",
            command=("pre-commit", "clean"),
            size=_cache_size,
        ),
    ),
)
