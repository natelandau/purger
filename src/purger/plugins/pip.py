"""Plugin for the pip download and wheel cache."""

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    RiskLevel,
    program_matching,
    user_cache_folders,
)
from purger.sizing import any_path_exists


def pip_cache_globs() -> tuple[str, ...]:
    """List the pip cache contents to delete, as glob-safe patterns.

    Deleted by path so a cache left behind after pip itself is gone still gets
    cleaned. On Linux pip follows `XDG_CACHE_HOME`, and `~/.cache/pip` is kept
    for a cache written before the variable was set.

    Returns:
        tuple[str, ...]: Cache content patterns, without duplicates.
    """
    return tuple(f"{folder}/*" for folder in user_cache_folders("pip"))


PIP_CACHE_GLOBS = pip_cache_globs()


def pip_cache_exists() -> bool:
    """Report whether a pip cache exists, since the plugin does not need pip installed.

    Returns:
        bool: True when any cache path has contents.
    """
    return any_path_exists(PIP_CACHE_GLOBS)


PLUGIN = Plugin(
    name="pip",
    description="pip download and wheel cache",
    skip_unless=pip_cache_exists,
    skip_while_running=program_matching(r"pip(3(\.\d+)?)?"),
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.CAUTION,
            description="Wipe the pip cache. Packages download and build again on next install.",
            cleanup=DeletePaths(*PIP_CACHE_GLOBS),
        ),
    ),
)
