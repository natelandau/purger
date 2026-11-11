"""Plugin for the pnpm package store and registry metadata cache."""

from pathlib import Path

from nclutils.sh import run_command

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import program_matcher
from purger.sizing import SIZE_CMD_TIMEOUT_SECONDS, disk_usage, printed_dir_usage


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
    candidates = ((root / line.strip()).resolve() for line in output.splitlines() if line.strip())
    return [path for path in candidates if path.is_relative_to(root) and path.is_dir()]


def _stale_metadata_size() -> int:
    """Measure the metadata cache folders this pnpm version can no longer read.

    Returns:
        int: Allocated bytes of the folders `pnpm cache prune` would delete.
    """
    cache_dir = Path(
        run_command(["pnpm", "cache", "path"], timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout.strip()
    )
    output = run_command(
        ["pnpm", "cache", "prune", "--dry-run"], timeout=SIZE_CMD_TIMEOUT_SECONDS
    ).stdout
    return disk_usage(_stale_metadata_dirs(output, cache_dir=cache_dir))


PLUGIN = Plugin(
    name="pnpm",
    description="pnpm package store and outdated registry metadata",
    requires="pnpm",
    size_cmd=("pnpm", "store", "path"),
    parse_size=printed_dir_usage,
    # Package scripts such as `pnpm run dev` can run for hours and leave the store alone.
    busy_process=program_matcher(
        r"pnp[mx](\.c?js)?",
        ignore_subcommands=frozenset({"run", "run-script", "exec", "start", "test", "t"}),
    ),
    actions=(
        Action(
            name="prune",
            tier=Tier.SAFE,
            help="Remove packages that no project on this machine references.",
            command=("pnpm", "store", "prune"),
        ),
        Action(
            name="metadata-prune",
            tier=Tier.SAFE,
            help=(
                "Remove registry metadata in formats this pnpm can no longer read. "
                "Projects pinned to an older pnpm fetch it again."
            ),
            command=("pnpm", "cache", "prune", "--yes"),
            size=_stale_metadata_size,
            # The metadata cache lives outside the store that the footprint measures.
            outside_footprint=True,
        ),
    ),
)
