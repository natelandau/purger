"""Plugin for Docker images, containers, build cache, and volumes."""

import json
import logging
import re
from collections.abc import Callable

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeReportedByCommand,
)
from purger.runner import command_succeeds, probe_output
from purger.sizing import nonblank_lines, parse_human_size

DOCKER_DISK_USAGE_COMMAND = ("docker", "system", "df", "--format", "json")
_PERCENT_SUFFIX = re.compile(r"\s*\(\d+(?:\.\d+)?%\)\s*$")

_DOCKER_CONTEXT_HOST_COMMAND = (
    "docker",
    "context",
    "inspect",
    "--format",
    "{{.Endpoints.docker.Host}}",
)

logger = logging.getLogger(__name__)


def _engine_is_local() -> bool:
    """Report whether the active Docker context points at an engine on this machine.

    `docker context inspect` reads local configuration, honors `DOCKER_HOST` and
    `DOCKER_CONTEXT`, and never dials the engine. Only a unix socket counts as
    local, since a TCP or SSH endpoint, even on loopback, can tunnel to another host.

    Returns:
        bool: True when the endpoint is a `unix://` socket.
    """
    output = probe_output(_DOCKER_CONTEXT_HOST_COMMAND, timeout=10)
    host = None if output is None else output.strip()
    if host is not None and host.startswith("unix://"):
        return True
    logger.debug("skipping docker: the active context's endpoint is not local: %r", host)
    return False


def docker_engine_is_local_and_running() -> bool:
    """Report whether a Docker engine on this machine answers.

    A remote engine is never available, so a prune cannot reach another host.

    Returns:
        bool: True when the context is local and `docker info` succeeds.
    """
    return _engine_is_local() and command_succeeds(["docker", "info"], timeout=10)


def reclaimable_bytes_of(output: str, types: frozenset[str]) -> int:
    """Sum the reclaimable bytes of the given `docker system df` types.

    Args:
        output: JSON lines from `docker system df --format json`.
        types: Row types to include, such as `Images` or `Build Cache`.

    Returns:
        int: Total reclaimable bytes.
    """
    total = 0
    for line in nonblank_lines(output):
        row = json.loads(line)
        if row.get("Type") in types:
            total += parse_human_size(_PERCENT_SUFFIX.sub("", row["Reclaimable"]))
    return total


def sum_reclaimable_bytes(*types: str) -> Callable[[str], int]:
    """Build a size reader that sums the reclaimable bytes of the given row types.

    Args:
        *types: Row types to include, such as `Images` or `Build Cache`.
    """
    row_types = frozenset(types)

    def read_reclaimable_bytes(output: str) -> int:
        return reclaimable_bytes_of(output, row_types)

    return read_reclaimable_bytes


# The total size and the system action read the same rows, so their sizes agree.
system_prune_reclaimable_bytes = sum_reclaimable_bytes("Images", "Containers", "Build Cache")


PLUGIN = Plugin(
    name="docker",
    description="Docker images, containers, build cache, and volumes",
    requires_on_path="docker",
    skip_unless=docker_engine_is_local_and_running,
    total_size=SizeReportedByCommand(
        DOCKER_DISK_USAGE_COMMAND,
        read_size=system_prune_reclaimable_bytes,
    ),
    actions=(
        Action(
            name="builder",
            risk_level=RiskLevel.SAFE,
            description="Remove the build cache. Builds run slower once, then recover.",
            cleanup=RunCommand("docker", "builder", "prune", "--all", "--force"),
            size=SizeReportedByCommand(
                DOCKER_DISK_USAGE_COMMAND,
                read_size=sum_reclaimable_bytes("Build Cache"),
            ),
        ),
        Action(
            name="system",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove all unused images, stopped containers, unused networks, and build cache. "
                "Images must be pulled again before next use."
            ),
            cleanup=RunCommand("docker", "system", "prune", "--all", "--force"),
            size=SizeReportedByCommand(
                DOCKER_DISK_USAGE_COMMAND,
                read_size=system_prune_reclaimable_bytes,
            ),
        ),
        Action(
            name="volumes",
            risk_level=RiskLevel.DANGEROUS,
            description="Remove all unused volumes. Volume data such as databases is lost permanently.",
            cleanup=RunCommand("docker", "volume", "prune", "--all", "--force"),
            size=SizeReportedByCommand(
                DOCKER_DISK_USAGE_COMMAND,
                read_size=sum_reclaimable_bytes("Local Volumes"),
            ),
            adds_to_plugin_total_size=True,
        ),
    ),
)
