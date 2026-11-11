"""Plugin for the mise cache and unused tool versions."""

import json
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
)
from purger.sizing import SIZE_COMMAND_TIMEOUT_SECONDS, disk_usage

MISE_CACHE_DIRECTORY_COMMAND = ("mise", "cache", "path")


def prunable_install_paths(output: str) -> list[Path]:
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


def prunable_versions_size() -> int:
    """Measure the tool versions `mise prune` would remove.

    Returns:
        int: Allocated bytes of the prunable install directories.
    """
    output = run_command(
        ["mise", "ls", "--prunable", "--json"], timeout=SIZE_COMMAND_TIMEOUT_SECONDS
    ).stdout
    return disk_usage(prunable_install_paths(output))


PLUGIN = Plugin(
    name="mise",
    description="mise cache and unused tool versions",
    requires_on_path="mise",
    total_size=SizeOfDirectoryPrintedBy(MISE_CACHE_DIRECTORY_COMMAND),
    actions=(
        Action(
            name="cache-prune",
            risk_level=RiskLevel.SAFE,
            description="Remove cache files not accessed in the last 30 days.",
            cleanup=RunCommand("mise", "cache", "prune"),
            size=SizeUnknown(reason="mise cannot preview which cache files prune removes"),
        ),
        Action(
            name="prune",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Uninstall tool versions that no tracked config uses. "
                "A project that still needs one must install it again."
            ),
            cleanup=RunCommand("mise", "prune", "--yes"),
            size=SizeComputedBy(prunable_versions_size),
            adds_to_plugin_total_size=True,
        ),
        Action(
            name="cache-clear",
            risk_level=RiskLevel.DANGEROUS,
            description="Wipe the entire mise cache. Tool metadata and downloads are fetched again.",
            cleanup=RunCommand("mise", "cache", "clear"),
            size=SizeOfDirectoryPrintedBy(MISE_CACHE_DIRECTORY_COMMAND),
        ),
    ),
)
