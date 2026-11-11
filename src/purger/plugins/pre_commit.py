"""Plugin for the pre-commit hook cache."""

from pathlib import Path

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeComputedBy,
    SizeUnknown,
    folder_from_environment,
)
from purger.safety import invoking_home
from purger.sizing import disk_usage


def _cache_dir() -> Path:
    """Return the directory pre-commit stores hook repos in, following its own lookup order."""
    xdg_cache = folder_from_environment("XDG_CACHE_HOME", default="~/.cache")
    folder = Path(folder_from_environment("PRE_COMMIT_HOME", default=f"{xdg_cache}/pre-commit"))
    if folder.parts[0] == "~":
        return invoking_home().joinpath(*folder.parts[1:])
    # Resolves `~user` forms, which `invoking_home` does not cover.
    return folder.expanduser()


def pre_commit_cache_size() -> int:
    """Measure the pre-commit cache.

    Returns:
        int: Allocated bytes, or 0 when the cache does not exist.
    """
    return disk_usage([_cache_dir().resolve()])


PLUGIN = Plugin(
    name="pre-commit",
    description="pre-commit hook environments and repos",
    requires_on_path="pre-commit",
    total_size=SizeComputedBy(pre_commit_cache_size),
    actions=(
        Action(
            name="gc",
            risk_level=RiskLevel.SAFE,
            description="Remove hook repos no longer referenced by any config.",
            cleanup=RunCommand("pre-commit", "gc"),
            size=SizeUnknown(reason="pre-commit cannot preview which repos gc removes"),
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.DANGEROUS,
            description="Wipe the entire pre-commit cache. Hook environments are reinstalled on next run.",
            cleanup=RunCommand("pre-commit", "clean"),
            size=SizeComputedBy(pre_commit_cache_size),
        ),
    ),
)
