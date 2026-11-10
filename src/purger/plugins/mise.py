"""Plugin for the mise cache and unused tool versions."""

import json
from pathlib import Path

from nclutils.sh import run_command

from purger.plugin import Action, Plugin, Tier
from purger.sizing import SIZE_CMD_TIMEOUT_SECONDS, disk_usage, printed_dir_usage

_CACHE_DIR_CMD = ("mise", "cache", "path")


def _prunable_install_paths(output: str) -> list[Path]:
    """Read the install directories from `mise ls --prunable --json`.

    Args:
        output: JSON mapping each tool to a list of its prunable versions.

    Returns:
        list[Path]: Install directories of the versions `mise prune` would remove.
    """
    tools: dict[str, list[dict[str, object]]] = json.loads(output)
    return [
        Path(str(version["install_path"]))
        for versions in tools.values()
        for version in versions
        if version.get("install_path")
    ]


def _prunable_size() -> int:
    """Measure the tool versions `mise prune` would remove.

    Returns:
        int: Allocated bytes of the prunable install directories.
    """
    output = run_command(
        ["mise", "ls", "--prunable", "--json"], timeout=SIZE_CMD_TIMEOUT_SECONDS
    ).stdout
    return disk_usage(_prunable_install_paths(output))


PLUGIN = Plugin(
    name="mise",
    description="mise cache and unused tool versions",
    requires="mise",
    size_cmd=_CACHE_DIR_CMD,
    parse_size=printed_dir_usage,
    actions=(
        Action(
            name="cache-prune",
            tier=Tier.SAFE,
            help="Remove cache files not accessed in the last 30 days.",
            command=("mise", "cache", "prune"),
        ),
        Action(
            name="prune",
            tier=Tier.CAUTION,
            help=(
                "Uninstall tool versions that no tracked config uses. "
                "A project that still needs one must install it again."
            ),
            command=("mise", "prune", "--yes"),
            size=_prunable_size,
            outside_footprint=True,
        ),
        Action(
            name="cache-clear",
            tier=Tier.DANGEROUS,
            help="Wipe the entire mise cache. Tool metadata and downloads are fetched again.",
            command=("mise", "cache", "clear"),
            size_cmd=_CACHE_DIR_CMD,
            parse_size=printed_dir_usage,
        ),
    ),
)
