"""Plugin for the pnpm package store and registry metadata cache."""

from pathlib import Path

from nclutils.sh import run_command

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeComputedBy,
    SizeOfDirectoryPrintedBy,
    SizeUnknown,
    program_matching,
)
from purger.sizing import SIZE_COMMAND_TIMEOUT_SECONDS, disk_usage, nonblank_lines


def _stale_metadata_dirs(output: str, cache_dir: Path) -> list[Path]:
    """Pick the directories a `pnpm cache prune --dry-run` lists, so only real cache folders are sized.

    Lines that do not name an existing directory inside the cache, such as the
    summary line, are skipped.

    Args:
        output: Stdout of `pnpm cache prune --dry-run`.
        cache_dir: The directory `pnpm cache path` prints.

    Returns:
        list[Path]: Directories inside the cache that a prune would delete.
    """
    root = cache_dir.resolve()
    candidates = ((root / line).resolve() for line in nonblank_lines(output))
    return [path for path in candidates if path.is_relative_to(root) and path.is_dir()]


def stale_metadata_size() -> int:
    """Measure the metadata cache folders this pnpm version can no longer read.

    Returns:
        int: Allocated bytes of the folders `pnpm cache prune` would delete.
    """
    cache_dir = Path(
        run_command(["pnpm", "cache", "path"], timeout=SIZE_COMMAND_TIMEOUT_SECONDS).stdout.strip()
    )
    output = run_command(
        ["pnpm", "cache", "prune", "--dry-run"], timeout=SIZE_COMMAND_TIMEOUT_SECONDS
    ).stdout
    return disk_usage(_stale_metadata_dirs(output, cache_dir=cache_dir))


PLUGIN = Plugin(
    name="pnpm",
    description="pnpm package store and outdated registry metadata",
    requires_on_path="pnpm",
    total_size=SizeOfDirectoryPrintedBy(("pnpm", "store", "path")),
    # Package scripts such as `pnpm run dev` can run for hours and leave the store alone.
    skip_while_running=program_matching(
        r"pnp[mx](\.c?js)?",
        ignoring_subcommands=frozenset({"run", "run-script", "exec", "start", "test", "t"}),
    ),
    actions=(
        Action(
            name="prune",
            risk_level=RiskLevel.SAFE,
            description="Remove packages that no project on this machine references.",
            cleanup=RunCommand("pnpm", "store", "prune"),
            size=SizeUnknown(reason="pnpm cannot preview which packages store prune removes"),
        ),
        Action(
            name="metadata-prune",
            risk_level=RiskLevel.SAFE,
            description=(
                "Remove registry metadata in formats this pnpm can no longer read. "
                "Projects pinned to an older pnpm fetch it again."
            ),
            cleanup=RunCommand("pnpm", "cache", "prune", "--yes"),
            size=SizeComputedBy(stale_metadata_size),
            # The metadata cache lives outside the store that the total size measures.
            adds_to_plugin_total_size=True,
        ),
    ),
)
