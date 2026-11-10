"""Plugin for the pacman package cache, pruned with paccache from pacman-contrib."""

import re

from purger.plugin import Action, Plugin, Tier
from purger.sizing import parse_human_size

_SAVED = re.compile(r"disk space saved: ([\d.]+ ?[A-Za-z]*)")
_OLD_VERSIONS_DRY_RUN = ("paccache", "--dryrun", "--nocolor")
_UNINSTALLED_DRY_RUN = ("paccache", "--dryrun", "--uninstalled", "--keep", "0", "--nocolor")


def _saved_bytes(output: str) -> int:
    """Read the space a paccache dry run reports it would free.

    Args:
        output: Stdout of a `paccache --dryrun` run.

    Returns:
        int: Bytes from the "disk space saved" summary, or 0 when paccache
            found nothing to prune and printed no summary.
    """
    match = _SAVED.search(output)
    return parse_human_size(match.group(1)) if match else 0


PLUGIN = Plugin(
    name="pacman",
    description="pacman cached package files",
    platforms=frozenset({"linux"}),
    requires="paccache",
    paths=("/var/cache/pacman/pkg/*",),
    actions=(
        Action(
            name="old-versions",
            tier=Tier.SAFE,
            help=(
                "Remove all but the 3 newest cached versions of each package. "
                "Downgrading further needs a download."
            ),
            command=("paccache", "--remove"),
            # Under sudo, paccache never calls sudo itself, which could prompt mid-run.
            needs_sudo=True,
            size_cmd=_OLD_VERSIONS_DRY_RUN,
            parse_size=_saved_bytes,
        ),
        Action(
            name="uninstalled",
            tier=Tier.SAFE,
            help="Remove every cached version of packages that are no longer installed.",
            command=("paccache", "--remove", "--uninstalled", "--keep", "0"),
            needs_sudo=True,
            size_cmd=_UNINSTALLED_DRY_RUN,
            parse_size=_saved_bytes,
        ),
    ),
)
