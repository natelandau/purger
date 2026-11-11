"""Plugin for the pacman package cache, pruned with paccache from pacman-contrib."""

import re

from purger.plugin import (
    Action,
    OperatingSystem,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    SizeReportedByCommand,
)
from purger.sizing import parse_human_size

DISK_SPACE_SAVED = re.compile(r"disk space saved: ([\d.]+ ?[A-Za-z]*)")
OLD_VERSIONS_DRY_RUN_COMMAND = ("paccache", "--dryrun", "--nocolor")
UNINSTALLED_DRY_RUN_COMMAND = (
    "paccache",
    "--dryrun",
    "--uninstalled",
    "--keep",
    "0",
    "--nocolor",
)


def paccache_saved_bytes(output: str) -> int:
    """Read the space a paccache dry run reports it would free.

    Args:
        output: Stdout of a `paccache --dryrun` run.

    Returns:
        int: Bytes from the "disk space saved" summary, or 0 when paccache
            found nothing to prune and printed no summary.
    """
    match = DISK_SPACE_SAVED.search(output)
    return parse_human_size(match.group(1)) if match else 0


PLUGIN = Plugin(
    name="pacman",
    description="pacman cached package files",
    operating_systems=(OperatingSystem.LINUX,),
    requires_on_path="paccache",
    total_size=SizeOfPaths("/var/cache/pacman/pkg/*"),
    actions=(
        Action(
            name="old-versions",
            risk_level=RiskLevel.SAFE,
            description=(
                "Remove all but the 3 newest cached versions of each package. "
                "Downgrading further needs a download."
            ),
            # Under sudo, paccache never calls sudo itself, which could prompt mid-run.
            cleanup=RunCommand("paccache", "--remove", with_sudo=True),
            size=SizeReportedByCommand(
                OLD_VERSIONS_DRY_RUN_COMMAND, read_size=paccache_saved_bytes
            ),
        ),
        Action(
            name="uninstalled",
            risk_level=RiskLevel.SAFE,
            description="Remove every cached version of packages that are no longer installed.",
            cleanup=RunCommand(
                "paccache", "--remove", "--uninstalled", "--keep", "0", with_sudo=True
            ),
            size=SizeReportedByCommand(UNINSTALLED_DRY_RUN_COMMAND, read_size=paccache_saved_bytes),
        ),
    ),
)
