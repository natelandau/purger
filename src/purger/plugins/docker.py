"""Plugin for Docker images, containers, build cache, and volumes."""

import json
import re
from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.runner import command_succeeds
from purger.sizing import parse_human_size

_DF_CMD = ("docker", "system", "df", "--format", "json")
_PERCENT_SUFFIX = re.compile(r"\s*\(\d+(?:\.\d+)?%\)\s*$")

_REMOVABLE = frozenset({"Images", "Containers", "Build Cache"})


def _is_available() -> bool:
    """Report whether the Docker daemon answers.

    Returns:
        bool: True when `docker info` succeeds.
    """
    return command_succeeds(["docker", "info"], timeout=10)


def _reclaimable_from(output: str, types: frozenset[str]) -> int:
    """Sum the reclaimable bytes of the given `docker system df` types.

    Args:
        output: JSON lines from `docker system df --format json`.
        types: Row types to include, such as `Images` or `Build Cache`.

    Returns:
        int: Total reclaimable bytes.
    """
    total = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("Type") in types:
            total += parse_human_size(_PERCENT_SUFFIX.sub("", row["Reclaimable"]))
    return total


PLUGIN = Plugin(
    name="docker",
    description="Docker images, containers, build cache, and volumes",
    requires="docker",
    is_available=_is_available,
    size_cmd=_DF_CMD,
    parse_size=partial(_reclaimable_from, types=_REMOVABLE),
    actions=(
        Action(
            name="builder",
            tier=Tier.SAFE,
            help="Remove the build cache. Builds run slower once, then recover.",
            command=("docker", "builder", "prune", "--all", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=frozenset({"Build Cache"})),
        ),
        Action(
            name="system",
            tier=Tier.CAUTION,
            help=(
                "Remove all unused images, stopped containers, unused networks, and build cache. "
                "Images must be pulled again before next use."
            ),
            command=("docker", "system", "prune", "--all", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=_REMOVABLE),
        ),
        Action(
            name="volumes",
            tier=Tier.DANGEROUS,
            help="Remove all unused volumes. Volume data such as databases is lost permanently.",
            command=("docker", "volume", "prune", "--all", "--force"),
            size_cmd=_DF_CMD,
            parse_size=partial(_reclaimable_from, types=frozenset({"Local Volumes"})),
            outside_footprint=True,
        ),
    ),
)
