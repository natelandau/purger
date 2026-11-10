"""Plugin for Podman images, containers, build cache, and volumes."""

import json
from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.runner import command_succeeds

_DF_CMD = ("podman", "system", "df", "--format", "json")
_REMOVABLE = frozenset({"Images", "Containers"})


def _is_available() -> bool:
    """Report whether Podman answers, which on macOS needs a running machine.

    Returns:
        bool: True when `podman info` succeeds.
    """
    return command_succeeds(["podman", "info"], timeout=10)


def _reclaimable_from(output: str, types: frozenset[str]) -> int:
    """Sum the reclaimable bytes of the given `podman system df` types.

    Args:
        output: The JSON array from `podman system df --format json`.
        types: Row types to include, such as `Images` or `Local Volumes`.

    Returns:
        int: Total reclaimable bytes.
    """
    return sum(row["RawReclaimable"] for row in json.loads(output) if row.get("Type") in types)


PLUGIN = Plugin(
    name="podman",
    description="Podman images, containers, build cache, and volumes",
    requires="podman",
    is_available=_is_available,
    size_cmd=_DF_CMD,
    parse_size=partial(_reclaimable_from, types=_REMOVABLE),
    actions=(
        Action(
            name="system",
            tier=Tier.CAUTION,
            help=(
                "Remove all unused images, stopped containers, pods, networks, and build cache. "
                "Images must be pulled again before next use."
            ),
            command=("podman", "system", "prune", "--all", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=_REMOVABLE),
        ),
        Action(
            name="volumes",
            tier=Tier.DANGEROUS,
            help="Remove all unused volumes. Volume data such as databases is lost permanently.",
            command=("podman", "volume", "prune", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=frozenset({"Local Volumes"})),
            outside_footprint=True,
        ),
    ),
)
