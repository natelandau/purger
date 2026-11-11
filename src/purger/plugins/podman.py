"""Plugin for Podman images, containers, build cache, and volumes."""

import json
import logging
import os
from collections.abc import Callable

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeReportedByCommand,
)
from purger.runner import probe_output

PODMAN_DISK_USAGE_COMMAND = ("podman", "system", "df", "--format", "json")

logger = logging.getLogger(__name__)


def _json_output(argv: list[str]) -> object | None:
    """Run a podman query and parse its JSON output, or return None when either step fails."""
    output = probe_output(argv, timeout=10)
    if output is None:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        logger.debug("%s printed invalid JSON", " ".join(argv))
        return None


def _remote_is_local() -> bool:
    """Report whether the remote connection podman uses reaches a service on this machine.

    Podman picks the connection from `CONTAINER_HOST` or `CONTAINER_CONNECTION`,
    then the default connection. A `podman machine` VM counts as local, and so does
    a unix socket. Any other TCP or SSH URL counts as remote, even on loopback,
    since it can tunnel to another host.

    Returns:
        bool: True when the connection is a podman machine or a unix socket.
    """
    url = os.environ.get("CONTAINER_HOST", "")
    wanted = os.environ.get("CONTAINER_CONNECTION", "")
    # Which variable wins when both are set is undocumented, so both must be local.
    if url and not url.startswith("unix://"):
        return False
    if url and not wanted:
        return True
    connections = _json_output(["podman", "system", "connection", "list", "--format", "json"])
    if not isinstance(connections, list):
        return False
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        if (conn.get("Name") == wanted) if wanted else conn.get("Default") is True:
            return conn.get("IsMachine") is True or str(conn.get("URI", "")).startswith("unix://")
    return False


def podman_service_is_local_and_running() -> bool:
    """Report whether a Podman service on this machine answers.

    On macOS that needs a running podman machine. A service on another host is
    never available, so a prune cannot reach it.

    Returns:
        bool: True when `podman info` succeeds and its service is local.
    """
    info = _json_output(["podman", "info", "--format", "json"])
    if not isinstance(info, dict):
        return False
    host = info.get("host")
    if isinstance(host, dict) and host.get("serviceIsRemote") is False:
        return True
    if _remote_is_local():
        return True
    logger.debug("skipping podman: its connection does not reach this machine")
    return False


def reclaimable_bytes_of(output: str, types: frozenset[str]) -> int:
    """Sum the reclaimable bytes of the given `podman system df` types.

    Args:
        output: The JSON array from `podman system df --format json`.
        types: Row types to include, such as `Images` or `Local Volumes`.

    Returns:
        int: Total reclaimable bytes.
    """
    return sum(row["RawReclaimable"] for row in json.loads(output) if row.get("Type") in types)


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
system_prune_reclaimable_bytes = sum_reclaimable_bytes("Images", "Containers")


PLUGIN = Plugin(
    name="podman",
    description="Podman images, containers, build cache, and volumes",
    requires_on_path="podman",
    skip_unless=podman_service_is_local_and_running,
    total_size=SizeReportedByCommand(
        PODMAN_DISK_USAGE_COMMAND,
        read_size=system_prune_reclaimable_bytes,
    ),
    actions=(
        Action(
            name="system",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove all unused images, stopped containers, pods, networks, and build cache. "
                "Images must be pulled again before next use."
            ),
            cleanup=RunCommand("podman", "system", "prune", "--all", "--force"),
            size=SizeReportedByCommand(
                PODMAN_DISK_USAGE_COMMAND,
                read_size=system_prune_reclaimable_bytes,
            ),
        ),
        Action(
            name="volumes",
            risk_level=RiskLevel.DANGEROUS,
            description="Remove all unused volumes. Volume data such as databases is lost permanently.",
            cleanup=RunCommand("podman", "volume", "prune", "--force"),
            size=SizeReportedByCommand(
                PODMAN_DISK_USAGE_COMMAND,
                read_size=sum_reclaimable_bytes("Local Volumes"),
            ),
            adds_to_plugin_total_size=True,
        ),
    ),
)
