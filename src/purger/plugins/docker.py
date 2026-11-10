"""Plugin for Docker images, containers, build cache, and volumes."""

import json
import logging
import re
from functools import partial

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import Action, Plugin, Tier
from purger.runner import command_succeeds
from purger.sizing import parse_human_size

_DF_CMD = ("docker", "system", "df", "--format", "json")
_PERCENT_SUFFIX = re.compile(r"\s*\(\d+(?:\.\d+)?%\)\s*$")

_REMOVABLE = frozenset({"Images", "Containers", "Build Cache"})
_CONTEXT_HOST_CMD = ("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")

logger = logging.getLogger(__name__)


def _engine_is_local() -> bool:
    """Report whether the active Docker context points at an engine on this machine.

    `docker context inspect` reads local configuration, honors `DOCKER_HOST` and
    `DOCKER_CONTEXT`, and never dials the engine. Only a unix socket counts as
    local, since a TCP or SSH endpoint, even on loopback, can tunnel to another host.

    Returns:
        bool: True when the endpoint is a `unix://` socket.
    """
    try:
        result = run_command(list(_CONTEXT_HOST_CMD), check=False, timeout=10)
    except ShellCommandError as exc:
        logger.debug("cannot read the docker context: %s", exc)
        return False
    host = result.stdout.strip()
    if result.ok and host.startswith("unix://"):
        return True
    logger.debug("skipping docker: the active context's endpoint is not local: %r", host)
    return False


def _is_available() -> bool:
    """Report whether a Docker engine on this machine answers.

    A remote engine is never available, so a prune cannot reach another host.

    Returns:
        bool: True when the context is local and `docker info` succeeds.
    """
    return _engine_is_local() and command_succeeds(["docker", "info"], timeout=10)


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
